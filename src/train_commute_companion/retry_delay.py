_MAX_RETRY_DELAY_SECONDS = 999_999_999
_MAX_RETRY_DELAY_DIGITS = len(str(_MAX_RETRY_DELAY_SECONDS))


def retry_delay_seconds(value: object) -> int | None:
    if type(value) is int and 0 <= value <= _MAX_RETRY_DELAY_SECONDS:
        return value
    return None


def retry_delay_header(value: str) -> int | None:
    digits = value.strip()
    if len(digits) <= _MAX_RETRY_DELAY_DIGITS and digits.isascii() and digits.isdecimal():
        return retry_delay_seconds(int(digits))
    return None
