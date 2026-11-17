"""Parser + filter checks against saved proposal pages.   python tests/test_details.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lancers_bot import filters  # noqa: E402
from lancers_bot.details import parse_propose_page  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def test_project_with_nda():
    r = parse_propose_page(fixture("propose_start_project_nda.html"), "https://www.lancers.jp/work/propose_start/5614153")
    assert r["status"] == "ok", r
    i = r["info"]
    assert i["title"] == "要件定義や基本設計ができる方（1人月、約1年アサイン予定）"
    assert i["client_name"] == "vcvcvc / vc"
    assert i["withholding_tax"] == "源泉徴収をしない" and i["withholding"] is False
    assert i["budget"] == {"display": "300,000円 ~ 500,000円", "min": 300000, "max": 500000}
    assert i["recruit_start_at"] == "2026-10-30T15:04:00+09:00"
    assert i["recruit_end_at"] == "2026-11-06T15:04:00+09:00"
    assert i["desired_delivery"] == "2027-12-31"
    assert i["work_type"] == "プロジェクト"
    assert i["nda_required"] is True and i["attachments_required"] is False
    assert i["proposals_left"] == 10
    assert i["other"]["検収期間"] == "14 日以内"
    assert i["description"].startswith("■ 依頼の目的・背景\n\n【概要】\n大手ECサイト")
    assert "続きを読む" not in i["description"] and "隠す" not in i["description"]
    assert i["missing"] == []


def test_competition():
    r = parse_propose_page(fixture("propose_start_competition.html"))
    i = r["info"]
    assert i["work_type"] == "コンペ"
    assert i["budget"]["display"] == "当選報酬: 45,001円" and i["budget"]["max"] == 45001
    assert "desired_delivery" not in i
    assert i["attachments_required"] is True and i["nda_required"] is False
    assert "(.jpg)" in i["description"] and "<span>" not in i["description"]
    decision = filters.evaluate({"title": i["title"]}, i, {}, blocked=False)
    assert not decision["allowed"] and "コンペ" in decision["reasons"][0]


def test_login_redirect():
    assert parse_propose_page("<html></html>", "https://www.lancers.jp/user/login?ref=x")["status"] == "login"
    assert parse_propose_page("<html><body><main>この依頼は募集を終了しました</main></body></html>")["status"] == "unavailable"
    left = parse_propose_page("<html><body><header>For ランサー</header><main>提案できません 依頼者が退会しているため、この依頼に提案できません。 パッケージを探す</main></body></html>")
    assert left == {"status": "unavailable", "info": None, "error": "提案できません: 依頼者が退会しているため、この依頼に提案できません。"}


def test_filters():
    info = parse_propose_page(fixture("propose_start_project_nda.html"))["info"]
    info = {**info, "recruit_end_at": "2099-01-01T00:00:00+09:00"}
    assert filters.evaluate({}, info, {}, blocked=False)["allowed"]
    assert filters.evaluate({}, info, {}, blocked=True)["blocked"]
    assert not filters.evaluate({}, info, {"min_budget": 600000}, blocked=False)["allowed"]
    assert not filters.evaluate({}, info, {"exclude_keywords": ["ECサイト"]}, blocked=False)["allowed"]
    assert not filters.evaluate({}, {**info, "recruit_end_at": "2020-01-01T00:00:00+09:00"}, {}, blocked=False)["allowed"]
    beginner = {"title": "【初心者大歓迎】在宅ワーク", "description": "初心者OK\n急募"}
    for word in ("初心者大歓迎", "[初心者大歓迎]", "【初心者大歓迎】", " 初心者大歓迎 ", "初心者ok", "急募"):
        assert not filters.evaluate(beginner, info, {"exclude_keywords": [word]}, blocked=False)["allowed"], word
    assert filters.evaluate(beginner, info, {"exclude_keywords": ["[]", "上級者"]}, blocked=False)["allowed"]
    assert not filters.evaluate({"title": "ＰＨＰ改修"}, info, {"exclude_keywords": ["php"]}, blocked=False)["allowed"]
    assert not filters.evaluate({}, info, {"exclude_clients": ["vcvcvc"]}, blocked=False)["allowed"]
    assert not filters.evaluate({}, info, {"exclude_clients": ["ＶＣ"]}, blocked=False)["allowed"]
    assert not filters.evaluate({"client_name": "株式会社サンプル", "client_url": "https://www.lancers.jp/client/sample-co"}, {},
                                {"exclude_clients": ["sample-co"]}, blocked=False)["allowed"]
    assert filters.evaluate({}, info, {"exclude_clients": ["別のクライアント"]}, blocked=False)["allowed"]


def test_openrouter_request():
    from lancers_bot.proposals import request_body
    fast = request_body("x-ai/grok-4.7", [], "medium", "fast", 450000)
    assert fast["model"] == "x-ai/grok-4.7:nitro" and fast["reasoning"]["effort"] == "medium" and "provider" not in fast
    assert request_body("x-ai/grok-4.7", [], "xhigh", None, 64000)["max_tokens"] == 64000          # capped at the model's limit
    plain = request_body("openai/gpt-4o", [], None, "normal")
    assert plain["model"] == "openai/gpt-4o" and "reasoning" not in plain and "provider" not in plain


def test_openai_request():
    from lancers_bot.proposals import clean, openai_body, provider_of
    assert provider_of("sk-or-v1-abc") == "openrouter" and provider_of("sk-proj-abc") == "openai"
    body = openai_body("openai/gpt-5.5", "system text", "job text", "high", "fast", 128000, web_search=True)
    assert body["model"] == "gpt-5.5" and body["instructions"] == "system text" and body["input"] == "job text"
    assert body["reasoning"] == {"effort": "high"} and body["service_tier"] == "priority" and body["tools"] == [{"type": "web_search"}]
    assert body["max_output_tokens"] == 20000 and body["store"] is False and "temperature" not in body
    plain = openai_body("openai/gpt-4o", "s", "u", reasoning_model=False)
    assert plain["temperature"] == 0.7 and "reasoning" not in plain and "service_tier" not in plain and "tools" not in plain
    assert clean("御社のサイトを拝見しました ([example.com](https://example.com/a))。") == "御社のサイトを拝見しました。"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
