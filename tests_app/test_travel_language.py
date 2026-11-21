import asyncio
import json

import httpx
import pytest
from tcc_app.travel_language import LanguageError, TravelLanguage, language_settings

TEXT = "Utrecht tomorrow and back"
CONTEXT = {"today": "2026-11-21", "timezone": "Europe/Amsterdam"}
SCHEMA = {"type": "object", "required": ["action"]}
CHANGE = {"action": "create", "plan": {"destination": "UT"}}
QUESTION = "When do you need to arrive?"
TOKEN = "test-private-language-token"
URL = "https://language.example.test/interpret"
MODEL_URL = "http://ollama:11434/v1/chat/completions"


async def test_agent_receives_bounded_context_and_returns_a_proposal():
    settings = language_settings(
        {
            "TCC_TRAVEL_LANGUAGE": "agent",
            "TCC_TRAVEL_LANGUAGE_URL": URL,
            "TCC_TRAVEL_LANGUAGE_TOKEN": TOKEN,
        }
    )
    assert settings is not None
    assert TOKEN not in repr(settings)

    def provider(request):
        assert request.method == "POST"
        assert str(request.url) == URL
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {
            "version": 1,
            "text": TEXT,
            "context": CONTEXT,
            "change_schema": SCHEMA,
        }
        return httpx.Response(200, json={"change": CHANGE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await TravelLanguage(settings, client).interpret(
            TEXT, context=CONTEXT, change_schema=SCHEMA
        )
    assert result.change == CHANGE and result.question is None


async def test_compatible_model_gets_the_change_schema_and_can_ask_a_question():
    settings = language_settings(
        {
            "TCC_TRAVEL_LANGUAGE": "model",
            "TCC_TRAVEL_LANGUAGE_URL": MODEL_URL,
            "TCC_TRAVEL_LANGUAGE_MODEL": "local-model",
        }
    )
    assert settings is not None

    def provider(request):
        assert str(request.url) == MODEL_URL
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == "local-model"
        system, user = payload["messages"]
        assert system["role"] == "system" and json.dumps(SCHEMA) in system["content"]
        assert user["role"] == "user"
        assert json.loads(user["content"]) == {"text": TEXT, "context": CONTEXT}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"question": QUESTION})}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        result = await TravelLanguage(settings, client).interpret(
            TEXT, context=CONTEXT, change_schema=SCHEMA
        )
    assert result.question == QUESTION and result.change is None


@pytest.mark.parametrize(
    "values,field",
    [
        ({"TCC_TRAVEL_LANGUAGE": "other"}, "TCC_TRAVEL_LANGUAGE"),
        ({"TCC_TRAVEL_LANGUAGE_URL": ""}, "TCC_TRAVEL_LANGUAGE_URL"),
        ({"TCC_TRAVEL_LANGUAGE_URL": "https://u:secret@example.test"}, "TCC_TRAVEL_LANGUAGE_URL"),
        ({"TCC_TRAVEL_LANGUAGE_URL": "ftp://example.test"}, "TCC_TRAVEL_LANGUAGE_URL"),
        ({"TCC_TRAVEL_LANGUAGE_TOKEN": "contains secret"}, "TCC_TRAVEL_LANGUAGE_TOKEN"),
        ({"TCC_TRAVEL_LANGUAGE": "model"}, "TCC_TRAVEL_LANGUAGE_MODEL"),
    ],
)
def test_bad_configuration_names_only_the_field(values, field):
    with pytest.raises(ValueError, match=field) as error:
        language_settings(
            {"TCC_TRAVEL_LANGUAGE": "agent", "TCC_TRAVEL_LANGUAGE_URL": URL, **values}
        )
    assert "secret" not in str(error.value)


def test_language_is_off_by_default_and_explicitly_disables_an_endpoint():
    assert language_settings({}) is None
    assert language_settings({"TCC_TRAVEL_LANGUAGE": "off", "TCC_TRAVEL_LANGUAGE_URL": URL}) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"change": CHANGE, "question": QUESTION},
        {"change": []},
        {"question": " "},
        {"question": "q" * 601},
        {"question": QUESTION, "extra": True},
        {"question": "q" * 32769},
        {"error": TOKEN},
    ],
)
async def test_malformed_output_returns_only_a_safe_error(payload):
    settings = language_settings({"TCC_TRAVEL_LANGUAGE": "agent", "TCC_TRAVEL_LANGUAGE_URL": URL})
    assert settings is not None
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(LanguageError) as error:
            await TravelLanguage(settings, client).interpret(
                TEXT, context=CONTEXT, change_schema=SCHEMA
            )
    assert TOKEN not in str(error.value) and TEXT not in str(error.value)


async def test_configured_endpoint_cannot_redirect_the_request():
    settings = language_settings(
        {
            "TCC_TRAVEL_LANGUAGE": "agent",
            "TCC_TRAVEL_LANGUAGE_URL": URL,
            "TCC_TRAVEL_LANGUAGE_TOKEN": TOKEN,
        }
    )
    assert settings is not None
    calls = []

    def provider(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://elsewhere.example.test"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(provider), follow_redirects=True
    ) as client:
        with pytest.raises(LanguageError):
            await TravelLanguage(settings, client).interpret(
                TEXT, context=CONTEXT, change_schema=SCHEMA
            )
    assert calls == [URL]


async def test_language_request_has_a_total_deadline(monkeypatch):
    monkeypatch.setattr("tcc_app.travel_language._DEADLINE", 0.01)
    settings = language_settings({"TCC_TRAVEL_LANGUAGE": "agent", "TCC_TRAVEL_LANGUAGE_URL": URL})
    assert settings is not None
    started = asyncio.Event()

    async def provider(_request):
        started.set()
        await asyncio.Event().wait()
        pytest.fail("The pending HTTP call must have been cancelled.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(LanguageError):
            await asyncio.wait_for(
                TravelLanguage(settings, client).interpret(
                    TEXT, context=CONTEXT, change_schema=SCHEMA
                ),
                0.5,
            )
    assert started.is_set()
