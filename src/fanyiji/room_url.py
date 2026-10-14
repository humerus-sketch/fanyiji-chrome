"""Normalize a Bilibili room reference without starting any browser."""
def room_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_room")
    value = value.strip()
    prefix = "https://live.bilibili.com/"
    if value.startswith(prefix):
        value = value[len(prefix):].split("?", 1)[0].rstrip("/")
    if not value.isascii() or not value.isdigit() or not 1 <= len(value) <= 12 or int(value) <= 0:
        raise ValueError("invalid_room")
    return prefix + str(int(value))
