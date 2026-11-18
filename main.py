
import os
import re
import json
import html
import logging
from urllib.parse import (
    urlparse,
    urljoin,
    unquote,
)

import requests
from flask import Flask, request, jsonify


# =========================
# 基础配置
# =========================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Mobile) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

# 可选：自己的抖音 Cookie。
# 不填写也可以尝试解析公开视频，但成功率可能受抖音风控影响。
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", "").strip()

ALLOWED_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
)

URL_PATTERN = re.compile(r"https?://[^\s，,。；;）)\]】\"'<>]+")


# =========================
# 请求会话
# =========================

def create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    if DOUYIN_COOKIE:
        session.headers["Cookie"] = DOUYIN_COOKIE

    return session


def is_douyin_domain(host):
    """只允许访问抖音自己的域名，避免任意 URL 请求。"""
    if not host:
        return False

    host = host.lower().split(":")[0]

    return any(
        host == domain or host.endswith("." + domain)
        for domain in ALLOWED_DOMAINS
    )


def safe_get(session, url, max_redirects=8):
    """手动跟随重定向，只访问允许的抖音域名。"""

    current_url = url

    for _ in range(max_redirects + 1):
        parsed = urlparse(current_url)

        if parsed.scheme not in ("http", "https"):
            raise ValueError("链接协议不合法")

        if not is_douyin_domain(parsed.hostname):
            raise ValueError("只支持抖音分享链接")

        response = session.get(
            current_url,
            timeout=TIMEOUT,
            allow_redirects=False,
        )

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")

            if not location:
                raise ValueError("抖音返回了无效的跳转地址")

            current_url = urljoin(current_url, location)
            continue

        response.raise_for_status()
        return response

    raise ValueError("分享链接跳转次数过多")


# =========================
# 提取分享链接
# =========================

def extract_share_url(text):
    """从分享文案中提取第一个 HTTP 链接。"""

    if not text:
        return None

    match = URL_PATTERN.search(text)

    if not match:
        return None

    return match.group(0).rstrip(".,!?:;，。；！？")


def extract_video_id(url):
    """从抖音链接中提取视频 ID。"""

    parsed = urlparse(url)

    patterns = [
        r"/video/(\d+)",
        r"/share/video/(\d+)",
        r"/discover/(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, parsed.path)

        if match:
            return match.group(1)

    match = re.search(
        r"(?:modal_id|aweme_id)=(\d+)",
        parsed.query,
    )

    if match:
        return match.group(1)

    return None


def resolve_video_id(session, share_url):
    """处理短链接，获得视频 ID。"""

    parsed = urlparse(share_url)

    if not is_douyin_domain(parsed.hostname):
        raise ValueError("这不是有效的抖音链接")

    # 完整视频链接可以直接提取 ID
    video_id = extract_video_id(share_url)

    if video_id:
        return video_id

    # 短链接需要跟随跳转
    response = safe_get(session, share_url)

    video_id = extract_video_id(response.url)

    if video_id:
        return video_id

    video_id = extract_video_id(response.text)

    if video_id:
        return video_id

    raise ValueError("无法从分享链接中获取视频 ID")


# =========================
# 页面 JSON 提取
# =========================

def collect_json_objects(value, results):
    """递归寻找可能包含视频信息的 JSON 对象。"""

    if isinstance(value, dict):
        results.append(value)

        for child in value.values():
            collect_json_objects(child, results)

    elif isinstance(value, list):
        for child in value:
            collect_json_objects(child, results)


def parse_json_text(text):
    """尝试解析 JSON 字符串。"""

    if not text:
        return None

    text = html.unescape(text.strip())

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def extract_page_json(page):
    """从 HTML 的脚本标签和页面变量中寻找 JSON。"""

    results = []

    # 常见页面数据变量
    patterns = [
        r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']SIGI_STATE["\'][^>]*>(.*?)</script>',
        r'<script[^>]+id=["\']_ROUTER_DATA["\'][^>]*>(.*?)</script>',
    ]

    for pattern in patterns:
        matches = re.findall(
            pattern,
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )

        for raw in matches:
            raw = unquote(raw)

            data = parse_json_text(raw)

            if data is not None:
                collect_json_objects(data, results)

    # 其他脚本标签中的 JSON
    script_pattern = (
        r"<script[^>]*>(.*?)</script>"
    )

    scripts = re.findall(
        script_pattern,
        page,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for script in scripts:
        if not any(
            key in script
            for key in (
                "play_addr",
                "download_addr",
                "aweme_detail",
                "awemeDetail",
            )
        ):
            continue

        data = parse_json_text(script)

        if data is not None:
            collect_json_objects(data, results)

    return results


# =========================
# 视频信息提取
# =========================

def first_url(value):
    """从 URL 列表或字符串中获取第一个有效地址。"""

    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return value
        return ""

    if isinstance(value, list):
        for item in value:
            result = first_url(item)

            if result:
                return result

    if isinstance(value, dict):
        for key in ("url_list", "url", "uri"):
            if key in value:
                result = first_url(value[key])

                if result:
                    return result

    return ""


def clean_media_url(url):
    """清理视频地址中的转义字符。"""

    if not url:
        return ""

    url = url.replace("\\u002F", "/")
    url = url.replace("\\/", "/")
    url = html.unescape(url)

    return url.strip()


def extract_video_info(objects):
    """从递归找到的 JSON 对象中提取视频信息。"""

    for obj in objects:

        # 可能存在多层 aweme_detail 包装
        detail = obj.get("aweme_detail")

        if isinstance(detail, dict):
            candidates = [detail, obj]
        else:
            candidates = [obj]

        for item in candidates:

            video = item.get("video")

            if not isinstance(video, dict):
                continue

            # 优先使用 play_addr
            video_url = first_url(
                video.get("play_addr")
            )

            # 其次尝试 download_addr
            if not video_url:
                video_url = first_url(
                    video.get("download_addr")
                )

            video_url = clean_media_url(video_url)

            if not video_url:
                continue

            title = (
                item.get("desc")
                or item.get("title")
                or ""
            )

            cover = ""

            cover = first_url(
                video.get("origin_cover")
            )

            if not cover:
                cover = first_url(
                    video.get("cover")
                )

            if not cover:
                cover = first_url(
                    video.get("dynamic_cover")
                )

            return {
                "title": str(title).strip(),
                "videoUrl": video_url,
                "coverUrl": clean_media_url(cover),
            }

    return None


# =========================
# 核心解析功能
# =========================

def parse_douyin(share_text):
    """解析抖音分享文案或视频链接。"""

    share_url = extract_share_url(share_text)

    if not share_url:
        raise ValueError("没有找到有效的抖音分享链接")

    session = create_session()

    try:
        video_id = resolve_video_id(
            session,
            share_url,
        )

        # 两种公开页面形式，依次尝试
        page_urls = [
            f"https://www.iesdouyin.com/share/video/{video_id}/",
            f"https://www.douyin.com/video/{video_id}",
        ]

        last_error = None

        for page_url in page_urls:

            try:
                response = safe_get(
                    session,
                    page_url,
                )

                page = response.text

                # 页面可能返回验证码或风控提示
                if any(
                    word in page.lower()
                    for word in (
                        "captcha",
                        "verifycenter",
                    )
                ):
                    last_error = "抖音触发了访问验证"
                    continue

                objects = extract_page_json(page)

                result = extract_video_info(objects)

                if result:
                    return {
                        "success": True,
                        **result,
                    }

                last_error = "页面中没有找到视频播放地址"

            except requests.RequestException as exc:
                logger.warning(
                    "请求抖音页面失败：%s",
                    exc,
                )
                last_error = "请求抖音页面失败，请稍后再试"

        raise ValueError(
            last_error or "暂时无法解析该视频"
        )

    finally:
        session.close()


# =========================
# Flask API
# =========================

@app.after_request
def add_cors_headers(response):
    """允许现有网页跨域访问 Flask API。"""

    response.headers["Access-Control-Allow-Origin"] = "*"

    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type"
    )

    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, OPTIONS"
    )

    return response


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "success": True,
        "message": "Python 视频解析服务运行正常",
    })


@app.route("/api/parse", methods=["GET", "POST", "OPTIONS"])
def api_parse():

    if request.method == "OPTIONS":
        return "", 204

    if request.method == "POST":

        data = request.get_json(silent=True) or {}

        share_text = (
            data.get("text")
            or data.get("url")
            or data.get("share_url")
            or data.get("shareText")
            or ""
        )

    else:

        share_text = (
            request.args.get("url")
            or request.args.get("text")
            or ""
        )

    if not share_text:
        return jsonify({
            "success": False,
            "message": "请输入抖音分享链接",
        }), 400

    try:

        result = parse_douyin(share_text)

        return jsonify(result)

    except ValueError as exc:

        return jsonify({
            "success": False,
            "message": str(exc),
        }), 400

    except requests.Timeout:

        return jsonify({
            "success": False,
            "message": "请求抖音超时，请稍后再试",
        }), 504

    except requests.RequestException as exc:

        logger.exception("网络请求异常")

        return jsonify({
            "success": False,
            "message": "访问抖音失败，请检查 VPS 网络",
        }), 502

    except Exception:

        logger.exception("解析程序发生异常")

        return jsonify({
            "success": False,
            "message": "服务器解析发生错误",
        }), 500


# =========================
# 启动服务
# =========================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )