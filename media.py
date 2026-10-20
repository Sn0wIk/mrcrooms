"""Parse VK links into official embeds without fetching arbitrary URLs."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit


def parse_vk(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Вставьте ссылку на видео VK или VK Видео.")
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {
                "vk.com", "www.vk.com", "m.vk.com",
                "vk.ru", "www.vk.ru", "m.vk.ru",
                "vkvideo.ru", "www.vkvideo.ru", "m.vkvideo.ru",
            }
            or parsed.username or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError
        query = parse_qs(parsed.query)
        if parsed.path == "/video_ext.php":
            owner = query.get("oid", [""])[0]
            video = query.get("id", [""])[0]
        else:
            # VK's share URLs also use ?z=video-123_456%2F...
            target = query.get("z", [parsed.path.lstrip("/")])[0].split("/")[0]
            match = re.fullmatch(r"video(-?\d{1,20})_(\d{1,20})", target)
            if not match:
                raise ValueError
            owner, video = match.groups()
        if not re.fullmatch(r"-?\d{1,20}", owner) or not re.fullmatch(r"\d{1,20}", video):
            raise ValueError
        params = {"oid": owner, "id": video, "js_api": "1"}
        if query.get("hash"):
            token = query["hash"][0]
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", token):
                raise ValueError
            params["hash"] = token
        return {
            "url": f"https://vkvideo.ru/video{owner}_{video}",
            "embed": "https://vk.ru/video_ext.php?" + urlencode(params),
            "label": f"VK Видео · {owner}_{video}",
        }
    except (ValueError, TypeError):
        raise ValueError(
            "Не удалось разобрать ссылку. Нужна ссылка вида "
            "https://vkvideo.ru/video-123_456 или VK video_ext.php."
        ) from None
