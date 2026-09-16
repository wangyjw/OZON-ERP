"""
M2 发布前置校验（preflight）测试

覆盖：
- check_store_ready：不存在/cookie/expired/pending/缺凭证/active 各分支
- preflight_product：商品不存在、缺类目、缺必填字段、payload 校验透传
- preflight：批量入口 + storeReady 聚合
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services import publish_preflight_service as pf


def _make_store(**kw):
    base = {
        'id': 1, 'store_id': 'S-1', 'alias': '测试店',
        'auth_type': 'api', 'auth_status': 'active',
        'client_id': 'cid', 'api_key': 'enc$v1$xx$yy',
        'currency': 'RUB',
    }
    base.update(kw)
    return base


def _make_product(**kw):
    base = {
        'id': 'p1', 'title': '测试商品',
        'price': 100, 'weight': 500,
        'descriptionCategoryId': 17028649, 'typeId': 91565,
        'images': ['https://cdn.ozoncdn.ru/img/1.jpg'],
        'skus': [{'skuCode': 'SKU-1', 'price': 100}],
        'attributes': [],
    }
    base.update(kw)
    return base


class CheckStoreReadyTests(unittest.TestCase):
    """店铺就绪度检查的各分支"""

    def test_no_store_id(self):
        ready, err, store = pf.check_store_ready(None)
        self.assertFalse(ready)
        self.assertIn('未指定', err)
        self.assertIsNone(store)

    @patch('models.account.Store.find_by_store_id', return_value=None)
    def test_store_not_found(self, _):
        ready, err, _ = pf.check_store_ready('S-X')
        self.assertFalse(ready)
        self.assertIn('不存在', err)

    @patch('models.account.Store.find_by_store_id')
    def test_cookie_store_rejected(self, mock_find):
        mock_find.return_value = _make_store(auth_type='cookie')
        ready, err, _ = pf.check_store_ready('S-1')
        self.assertFalse(ready)
        self.assertIn('Cookie', err)

    @patch('models.account.Store.find_by_store_id')
    def test_expired_store_blocked(self, mock_find):
        mock_find.return_value = _make_store(
            auth_status='expired', last_auth_error='401 认证失败')
        ready, err, _ = pf.check_store_ready('S-1')
        self.assertFalse(ready)
        self.assertIn('凭证失效', err)
        self.assertIn('401', err)

    @patch('models.account.Store.find_by_store_id')
    def test_pending_store_blocked(self, mock_find):
        mock_find.return_value = _make_store(auth_status='pending')
        ready, err, _ = pf.check_store_ready('S-1')
        self.assertFalse(ready)
        self.assertIn('未授权', err)

    @patch('models.account.Store.find_by_store_id')
    def test_active_store_without_credentials_blocked(self, mock_find):
        mock_find.return_value = _make_store(client_id=None)
        ready, err, _ = pf.check_store_ready('S-1')
        self.assertFalse(ready)
        self.assertIn('Client-Id', err)

    @patch('models.account.Store.find_by_store_id')
    def test_active_api_store_ok(self, mock_find):
        mock_find.return_value = _make_store()
        ready, err, store = pf.check_store_ready('S-1')
        self.assertTrue(ready)
        self.assertIsNone(err)
        self.assertEqual('S-1', store['store_id'])


class PreflightProductTests(unittest.TestCase):
    """单商品预检"""

    @patch.object(pf.Product, 'find_by_id', return_value=None)
    def test_product_not_found(self, _):
        r = pf.preflight_product('missing', 'S-1')
        self.assertFalse(r['ready'])
        self.assertIn('商品不存在', r['blockers'])

    @patch.object(pf.Product, 'find_by_id')
    def test_missing_category_is_blocker(self, mock_find):
        mock_find.return_value = _make_product(
            descriptionCategoryId=None, typeId=None)
        r = pf.preflight_product('p1', 'S-1')
        self.assertFalse(r['ready'])
        self.assertTrue(any('类目' in b for b in r['blockers']))

    @patch.object(pf, 'validate_ozon_product_items')
    @patch.object(pf, 'build_ozon_product_items', return_value=[{'offer_id': 'x'}])
    @patch.object(pf.Product, 'find_by_id')
    def test_missing_title_and_price_are_blockers(
            self, mock_find, _build, _validate):
        mock_find.return_value = _make_product(title='', price=0)
        _validate.return_value = {'valid': True, 'errors': [], 'warnings': []}
        r = pf.preflight_product('p1', 'S-1')
        self.assertFalse(r['ready'])
        joined = '|'.join(r['blockers'])
        self.assertIn('标题', joined)
        self.assertIn('售价', joined)

    @patch.object(pf, 'validate_ozon_product_items')
    @patch.object(pf, 'build_ozon_product_items', return_value=[{'offer_id': 'x'}])
    @patch.object(pf.Product, 'find_by_id')
    def test_schema_errors_become_blockers(self, mock_find, _build, mock_validate):
        mock_find.return_value = _make_product()
        mock_validate.return_value = {
            'valid': False,
            'errors': ['item[1] 缺少必填属性: Бренд (85)'],
            'warnings': ['warn-1'],
        }
        r = pf.preflight_product('p1', 'S-1')
        self.assertFalse(r['ready'])
        self.assertIn('item[1] 缺少必填属性: Бренд (85)', r['blockers'])
        self.assertIn('warn-1', r['warnings'])

    @patch.object(pf, 'validate_ozon_product_items')
    @patch.object(pf, 'build_ozon_product_items', return_value=[{'offer_id': 'x'}])
    @patch.object(pf.Product, 'find_by_id')
    def test_clean_product_ready(self, mock_find, _build, mock_validate):
        mock_find.return_value = _make_product()
        mock_validate.return_value = {'valid': True, 'errors': [], 'warnings': []}
        r = pf.preflight_product('p1', 'S-1')
        self.assertTrue(r['ready'])
        self.assertEqual([], r['blockers'])

    @patch.object(pf, 'validate_ozon_product_items')
    @patch.object(pf, 'build_ozon_product_items', return_value=[{'offer_id': 'x'}])
    @patch.object(pf.Product, 'find_by_id')
    def test_data_uri_image_warns_not_blocks(self, mock_find, _build, mock_validate):
        mock_find.return_value = _make_product(
            images=['data:image/jpeg;base64,AAAA'])
        mock_validate.return_value = {'valid': True, 'errors': [], 'warnings': []}
        r = pf.preflight_product('p1', 'S-1')
        self.assertTrue(r['ready'])
        self.assertTrue(any('base64' in w for w in r['warnings']))

    @patch.object(pf, 'build_ozon_product_items', side_effect=RuntimeError('组装炸'))
    @patch.object(pf.Product, 'find_by_id')
    def test_build_exception_is_blocker(self, mock_find, _build):
        mock_find.return_value = _make_product()
        r = pf.preflight_product('p1', 'S-1')
        self.assertFalse(r['ready'])
        self.assertTrue(any('组装失败' in b for b in r['blockers']))


class PreflightBatchTests(unittest.TestCase):
    """批量入口聚合"""

    @patch.object(pf, 'check_store_ready', return_value=(True, None, _make_store()))
    @patch.object(pf, 'preflight_product')
    @patch.object(pf, '_get_store_currency', return_value='RUB')
    def test_aggregates_store_and_items(self, _cur, mock_item, _ready):
        mock_item.side_effect = [
            {'productId': 'p1', 'ready': True, 'blockers': [], 'warnings': []},
            {'productId': 'p2', 'ready': False, 'blockers': ['x'], 'warnings': []},
        ]
        r = pf.preflight(['p1', 'p2'], 'S-1')
        self.assertTrue(r['storeReady'])
        self.assertIsNone(r['storeError'])
        self.assertEqual('RUB', r['storeCurrency'])
        self.assertEqual(2, len(r['items']))
        self.assertTrue(r['items'][0]['ready'])
        self.assertFalse(r['items'][1]['ready'])

    @patch.object(pf, 'check_store_ready',
                  return_value=(False, '店铺凭证失效', None))
    @patch.object(pf, 'preflight_product',
                  return_value={'productId': 'p1', 'ready': True,
                                'blockers': [], 'warnings': []})
    def test_store_not_ready_still_checks_items(self, _item, _ready):
        # 店铺不就绪也要照常跑商品检查（让用户一次看清全部问题）
        r = pf.preflight(['p1'], 'S-1')
        self.assertFalse(r['storeReady'])
        self.assertIn('凭证失效', r['storeError'])
        self.assertIsNone(r['storeCurrency'])
        self.assertEqual(1, len(r['items']))


if __name__ == '__main__':
    unittest.main()
