"""
M1 绑店闭环：凭证校验链路测试

覆盖：
- ozon_api.get_store_info：/v1/seller/info 首选（官方 SellerAPI_SellerInfo）+ /v3/product/list 降级
- store_routes._verify_credentials：错误码 → 中文提示映射
- store_routes._apply_verify_result：成功/失败写回 stores 表
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# get_store_info 依赖 _call_ozon_api → requests.post；测试中统一 mock 掉
from services import ozon_api
from services.ozon_api import get_store_info, OzonAPIError


def _mock_resp(status_code=200, payload=None):
    """构造 requests.Response 风格的 mock"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else {}
    resp.text = str(payload)
    return resp


class GetStoreInfoTests(unittest.TestCase):
    """get_store_info：首选 seller_info，404/405 降级 product_list，401/403 直接抛"""

    @patch('services.ozon_api.requests.post')
    def test_seller_info_success_returns_valid(self, mock_post):
        # 官方 /v1/seller/info 响应：{company:{name,legal_name,currency,...}, ratings, subscription}
        mock_post.return_value = _mock_resp(200, {
            'company': {'name': "ООО 'Ромашка'", 'currency': 'RUB', 'country': 'Россия'},
            'subscription': {'is_premium': True, 'type': 'Профессиональный'},
        })
        info = get_store_info(client_id='cid', api_key='key')
        self.assertTrue(info['valid'])
        self.assertIsNone(info['store_id'])          # seller/info 不返回店铺数字 ID
        self.assertEqual("ООО 'Ромашка'", info['name'])
        self.assertEqual('RUB', info['currency'])
        self.assertEqual('seller_info', info['probe'])
        # 确认只调用了 seller/info 端点
        self.assertEqual(1, mock_post.call_count)
        self.assertIn('/v1/seller/info', mock_post.call_args[0][0])

    @patch('services.ozon_api.requests.post')
    def test_seller_info_404_falls_back_to_product_list(self, mock_post):
        mock_post.side_effect = [
            _mock_resp(404, {'message': 'not found'}),
            _mock_resp(200, {'result': {'total': 7}}),
        ]
        info = get_store_info(client_id='cid', api_key='key')
        self.assertTrue(info['valid'])
        self.assertEqual('product_list', info['probe'])
        self.assertEqual(7, info['total'])
        self.assertEqual(2, mock_post.call_count)
        self.assertIn('/v3/product/list', mock_post.call_args_list[1][0][0])

    @patch('services.ozon_api.requests.post')
    def test_401_raises_immediately_no_fallback(self, mock_post):
        mock_post.return_value = _mock_resp(401, {'message': 'unauthorized'})
        with self.assertRaises(OzonAPIError) as ctx:
            get_store_info(client_id='cid', api_key='bad')
        self.assertEqual(401, ctx.exception.status_code)
        # 401 是确定无效，不应触发降级请求
        self.assertEqual(1, mock_post.call_count)

    @patch('services.ozon_api.requests.post')
    def test_403_raises_immediately(self, mock_post):
        mock_post.return_value = _mock_resp(403, {'message': 'forbidden'})
        with self.assertRaises(OzonAPIError) as ctx:
            get_store_info(client_id='cid', api_key='key')
        self.assertEqual(403, ctx.exception.status_code)
        self.assertEqual(1, mock_post.call_count)

    @patch('services.ozon_api.requests.post')
    def test_500_does_not_fallback(self, mock_post):
        # 5xx 是服务端错误，非"端点不存在"，不应降级重试
        mock_post.return_value = _mock_resp(500, {'message': 'server error'})
        with self.assertRaises(OzonAPIError) as ctx:
            get_store_info(client_id='cid', api_key='key')
        self.assertEqual(500, ctx.exception.status_code)
        self.assertEqual(1, mock_post.call_count)


class VerifyCredentialsTests(unittest.TestCase):
    """_verify_credentials：OzonAPIError → 统一结果 dict 的中文提示映射"""

    def _verify(self, code, msg='err'):
        from routes.store_routes import _verify_credentials
        with patch.object(
            ozon_api.requests, 'post',
            return_value=_mock_resp(code, {'message': msg})
        ):
            return _verify_credentials('cid', 'key')

    def test_401_maps_to_auth_failed_message(self):
        r = self._verify(401)
        self.assertFalse(r['valid'])
        self.assertEqual(401, r['error_code'])
        self.assertIn('401', r['message'])
        self.assertIn('Client-Id', r['message'])

    def test_403_maps_to_permission_message(self):
        r = self._verify(403)
        self.assertFalse(r['valid'])
        self.assertEqual(403, r['error_code'])
        self.assertIn('403', r['message'])

    def test_success_returns_valid_true(self):
        from routes.store_routes import _verify_credentials
        with patch.object(
            ozon_api.requests, 'post',
            return_value=_mock_resp(200, {
                'company': {'name': 'S', 'currency': 'RUB'},
            })
        ):
            r = _verify_credentials('cid', 'key')
        self.assertTrue(r['valid'])
        self.assertEqual('S', r['store_name'])
        self.assertIsNone(r['error_code'])


class ApplyVerifyResultTests(unittest.TestCase):
    """_apply_verify_result：校验结果写回 stores 表（真实 DB，用临时库隔离）"""

    @classmethod
    def setUpClass(cls):
        import db as db_mod
        cls._orig_path = db_mod.DB_PATH
        import tempfile
        cls._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        cls._tmp.close()
        db_mod.DB_PATH = cls._tmp.name
        db_mod.init_db()

    @classmethod
    def tearDownClass(cls):
        import db as db_mod
        db_mod.DB_PATH = cls._orig_path
        try:
            os.unlink(cls._tmp.name)
        except OSError:
            pass

    def _create_store(self, store_id='T-1'):
        from models.account import Store
        return Store.create(
            store_id=store_id, alias='测试店', currency='RUB',
            auth_type='api', client_id='cid', api_key='key',
            auth_status='pending',
        )

    def test_verify_success_sets_active_and_clears_error(self):
        from routes.store_routes import _apply_verify_result
        store = self._create_store('T-OK')
        verify = {'valid': True, 'store_id': 'T-OK', 'store_name': 'S', 'status': 'active'}
        updated = _apply_verify_result(store['id'], verify, user_store_id='T-OK')
        self.assertEqual('active', updated['auth_status'])
        self.assertIsNotNone(updated['verify_time'])
        self.assertIsNone(updated['last_auth_error'])
        self.assertIsNotNone(updated['auth_time'])

    def test_verify_failure_sets_expired_and_records_error(self):
        from routes.store_routes import _apply_verify_result
        store = self._create_store('T-FAIL')
        verify = {'valid': False, 'message': 'Ozon 认证失败（401）', 'error_code': 401}
        updated = _apply_verify_result(store['id'], verify, user_store_id='T-FAIL')
        self.assertEqual('expired', updated['auth_status'])
        self.assertIsNotNone(updated['verify_time'])
        self.assertIn('401', updated['last_auth_error'])

    def test_store_id_backfilled_when_ozon_returns_different_id(self):
        from routes.store_routes import _apply_verify_result
        store = self._create_store('T-OLD')
        verify = {'valid': True, 'store_id': 'T-NEW', 'store_name': 'S'}
        updated = _apply_verify_result(store['id'], verify, user_store_id='T-OLD')
        # Ozon 返回的真实 ID 与填写不一致 → 回填
        self.assertEqual('T-NEW', updated['store_id'])
        self.assertTrue(verify.get('store_id_updated'))


if __name__ == '__main__':
    unittest.main()
