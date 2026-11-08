import httpx
from runtime_rig import FakeClock, at
from tcc_app.telegram import TelegramApi, TelegramNotifier


def test_telegram_receipt_identity_tracks_actual_bot_and_chat_without_token():
    client = httpx.AsyncClient()
    clock = FakeClock(at("08:00"))

    def notifier(token, chat):
        return TelegramNotifier(TelegramApi(token, client), chat, clock.now)

    original = notifier("123:private-secret", 456).destination_id
    assert original == notifier("123:rotated-secret", 456).destination_id
    assert original != notifier("124:private-secret", 456).destination_id
    assert original != notifier("123:private-secret", 457).destination_id
    assert "private-secret" not in original and "456" not in original
    assert notifier("123:private-secret", None).destination_id is None
