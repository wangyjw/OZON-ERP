"""发布前置校验（preflight）服务

POST /api/publish/preflight 的实现：对「商品 + 目标店铺」做只读预检，
把可在点击瞬间拦截的问题（凭证失效、缺类目、缺必填属性、价格/SKU 异常、
图片不可公网访问）全部前置暴露，避免「提交 → 等待 → 异步失败」的长循环。

设计要点（见 docs/核心链路设计方案-采集绑店发布.md §4.2）：
- 只读预演：不建任务、不调 Ozon 写接口、不写任何存储
- 校验结果分两级：blockers（阻塞发布）/ warnings（可继续）
- 复用现有实现：validate_ozon_product_items（必填属性/字典值校验，带 6h 缓存）、
  build_ozon_product_items（真实组装一次 payload 再喂给校验器，最贴近发布时行为）
"""
import copy

from models.product import Product
from services.publish_service import (
    build_ozon_product_items,
    validate_ozon_product_items,
    _get_store_currency,
    _valid_skus,
    _normalize_product_category_ids,
    _apply_price_offset,
)

# 与 app.py / publish_service.py 保持一致的公网 CDN 白名单
_PUBLIC_CDN_DOMAINS = ('alicdn.com', 'taobaocdn.com', 'tbcdn.cn', 'ozoncdn.ru')


def _is_public_url(url):
    """公网可直接访问的图片 URL（Ozon 可拉取）"""
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return False
    if '://localhost' in url or '://127.0.0.1' in url:
        return False
    return any(d in url for d in _PUBLIC_CDN_DOMAINS)


def check_store_ready(store_id):
    """店铺就绪度：存在 + auth_type='api' + auth_status='active' + 有凭证

    Returns:
        (ready: bool, error: str|None, store: dict|None)
    """
    if not store_id:
        return False, '未指定发布店铺', None
    try:
        from models.account import Store
        store = Store.find_by_store_id(store_id)
    except Exception as e:
        return False, f'店铺查询失败: {e}', None
    if not store:
        return False, f'店铺 {store_id} 不存在', None

    auth_type = store.get('auth_type') or 'api'
    if auth_type == 'cookie':
        return False, 'Cookie 授权店铺不支持 API 发布，请补充 Client-Id/Api-Key', store

    auth_status = store.get('auth_status')
    if auth_status == 'expired':
        err = store.get('last_auth_error') or '凭证已失效'
        return False, f'店铺凭证失效：{err}，请到「店铺管理」重新授权', store
    if auth_status != 'active':
        return False, f'店铺未授权（当前状态: {auth_status or "未知"}）', store

    if not store.get('client_id') or not store.get('api_key'):
        return False, '店铺缺少 Client-Id 或 Api-Key', store

    return True, None, store


def _check_product_basic(product, blockers, warnings):
    """商品级基础字段校验（不联网）"""
    if not product.get('title'):
        blockers.append('缺少产品标题')

    price = product.get('price')
    try:
        if price in (None, '') or float(price) <= 0:
            blockers.append('售价未设置或小于等于 0')
    except (TypeError, ValueError):
        blockers.append(f'售价格式无效: {price}')

    if not product.get('weight') or float(product.get('weight') or 0) <= 0:
        blockers.append('包裹重量未设置或小于等于 0')

    # SKU 就绪度
    skus = _valid_skus(product)
    if not skus:
        # 无 SKU 的单商品仍可发布（build_ozon_product_item 回退到商品级 offer_id），
        # 但如果商品声明了 skus 字段却全部无效，大概率是数据问题
        declared = product.get('skus') or product.get('skuList') or product.get('variants')
        if declared:
            blockers.append('SKU 列表存在但全部无效（缺少 offer_id/price）')

    # 图片可发布性（warning 级：发布管线会自动转存，但提示用户可能变慢）
    images = product.get('images') or []
    if not images:
        blockers.append('缺少产品图片')
    else:
        non_public = [
            u for u in images
            if isinstance(u, str) and u and not _is_public_url(u) and not u.startswith('data:')
        ]
        data_imgs = [u for u in images if isinstance(u, str) and u.startswith('data:')]
        if data_imgs:
            warnings.append(f'{len(data_imgs)} 张图片为 base64 内嵌数据，发布时将自动转存（较慢）')
        if non_public:
            warnings.append(f'{len(non_public)} 张图片为非公网 URL，发布时将自动转存')


def _check_product_payload(product, store_id, publish_mode, blockers, warnings):
    """组装真实 payload 并跑 schema 校验（会调 get_category_attributes，带 6h 缓存）

    返回 ozon_items 供调用方诊断用（preflight 不真正用它）。
    """
    desc_id = product.get('descriptionCategoryId')
    type_id = product.get('typeId')
    if not desc_id or not type_id:
        blockers.append('未匹配 Ozon 类目，请先在编辑页选择类目')
        return None

    try:
        items = build_ozon_product_items(
            product, store_id=store_id, publish_mode=publish_mode)
    except Exception as e:
        blockers.append(f'商品数据组装失败: {e}')
        return None

    try:
        result = validate_ozon_product_items(product, items)
    except Exception as e:
        # schema 拉取失败不应阻塞 preflight 返回其他已查出的问题
        warnings.append(f'类目属性 schema 校验未完成: {e}')
        return items

    for err in result.get('errors') or []:
        blockers.append(err)
    for warn in result.get('warnings') or []:
        warnings.append(warn)
    return items


def preflight_product(product_id, store_id, publish_mode=None):
    """单商品预检

    Returns:
        {
          'productId': str,
          'ready': bool,
          'blockers': [str],
          'warnings': [str],
        }
    """
    blockers = []
    warnings = []

    product = Product.find_by_id(product_id)
    if not product:
        return {
            'productId': product_id,
            'ready': False,
            'blockers': ['商品不存在'],
            'warnings': [],
        }

    # 与发布路径一致的数据预处理（在副本上做，不写回）
    product = copy.deepcopy(product)
    _normalize_product_category_ids(product)

    _check_product_basic(product, blockers, warnings)
    _check_product_payload(product, store_id, publish_mode, blockers, warnings)

    return {
        'productId': product_id,
        'ready': not blockers,
        'blockers': blockers,
        'warnings': warnings,
    }


def preflight(product_ids, store_id, publish_mode=None):
    """批量预检入口

    Returns:
        {
          'storeReady': bool,
          'storeError': str|None,
          'storeCurrency': str|None,
          'items': [preflight_product(...)],
        }
    """
    store_ready, store_error, store = check_store_ready(store_id)
    currency = _get_store_currency(store_id) if store_ready else None

    items = [
        preflight_product(pid, store_id, publish_mode)
        for pid in (product_ids or [])
    ]

    return {
        'storeReady': store_ready,
        'storeError': store_error,
        'storeCurrency': currency,
        'items': items,
    }
