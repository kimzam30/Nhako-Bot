"""Web tools with a fake network (httpx.MockTransport) and a fake search engine."""
import httpx
import pytest

from nhako.agent import Agent
from nhako.tools import Registry, web

from .conftest import FakeLLM

ARTICLE = "<html><head><title>Compose startup order</title></head><body><nav>Home | Docs</nav><article>" + \
    "".join(f"<p>Paragraph {i}: Docker Compose starts services in dependency order. "
            f"Use depends_on with condition service_healthy to wait for a healthcheck.</p>" for i in range(8)) + \
    "</article><footer>© Docker</footer></body></html>"


def site(routes: dict):
    """A fake internet: url -> (status, headers, body)."""
    def handler(request: httpx.Request):
        status, headers, body = routes.get(str(request.url), (404, {}, b"nope"))
        return httpx.Response(status, headers=headers, content=body)
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Hosts ending in .lan count as private; everything else is public. No real DNS in tests."""
    async def check(url):
        if ".lan" in url:
            raise web.FetchError("that host is on a private network; I only read public pages.")
    monkeypatch.setattr(web, "check_public", check)


def read_tool(ctx, routes):
    return {t.name: t for t in web.tools(ctx, transport=site(routes))}["read_url"]


HTML = {"content-type": "text/html; charset=utf-8"}


async def test_read_page_extracts_main_text(ctx):
    tool = read_tool(ctx, {"https://docs.example.com/a": (200, HTML, ARTICLE.encode())})
    out = await tool.run({"url": "https://docs.example.com/a"}, ctx)
    assert out.summarize and "Compose startup order" in out.text
    assert "service_healthy" in out.text and "Home | Docs" not in out.text
    assert "untrusted" in out.text and out.footer == "🔗 https://docs.example.com/a"


async def test_bare_domain_gets_https(ctx):
    tool = read_tool(ctx, {"https://docs.example.com/a": (200, HTML, ARTICLE.encode())})
    assert (await tool.run({"url": "docs.example.com/a"}, ctx)).summarize


async def test_redirect_into_private_network_is_blocked(ctx):
    tool = read_tool(ctx, {"https://short.example/x": (302, {"location": "http://portainer.lan:9000/"}, b"")})
    out = await tool.run({"url": "https://short.example/x"}, ctx)
    assert not out.summarize and "private network" in out.text


async def test_normal_redirect_is_followed(ctx):
    tool = read_tool(ctx, {"https://short.example/x": (301, {"location": "https://docs.example.com/a"}, b""),
                           "https://docs.example.com/a": (200, HTML, ARTICLE.encode())})
    out = await tool.run({"url": "https://short.example/x"}, ctx)
    assert out.footer == "🔗 https://docs.example.com/a"


async def test_binary_files_are_refused(ctx):
    tool = read_tool(ctx, {"https://x.example/f.pdf": (200, {"content-type": "application/pdf"}, b"%PDF")})
    assert "application/pdf file" in (await tool.run({"url": "https://x.example/f.pdf"}, ctx)).text


async def test_http_errors_are_reported(ctx):
    out = await read_tool(ctx, {}).run({"url": "https://x.example/missing"}, ctx)
    assert "answered 404" in out.text


async def test_javascript_only_page(ctx):
    body = b"<html><body><div id=root></div><script>app()</script></body></html>"
    out = await read_tool(ctx, {"https://spa.example/": (200, HTML, body)}).run({"url": "https://spa.example/"}, ctx)
    assert "almost no readable text" in out.text


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://192.168.1.20:9000/", "http://[::1]/",
                                 "http://10.0.0.5/", "file:///etc/passwd", "ftp://example.com/"])
async def test_check_public_refuses_local_addresses(url, monkeypatch):
    monkeypatch.undo()  # the real check, on IP literals (no DNS needed)
    with pytest.raises(web.FetchError):
        await web.check_public(url)


async def test_check_public_allows_public_ip(monkeypatch):
    monkeypatch.undo()
    await web.check_public("https://1.1.1.1/")


def test_pick_passages_short_text_untouched():
    assert web.pick_passages("hello", budget=100) == ("hello", False)


def test_pick_passages_keeps_relevant_paragraphs_in_order():
    paras = ["Intro about the product."] + [f"Filler paragraph {i} " * 5 for i in range(20)] + \
            ["The warranty lasts two years in Malaysia."]
    text, cut = web.pick_passages("\n".join(paras), "how long is the warranty", budget=300)
    assert cut and text.startswith("Intro") and "warranty lasts two years" in text and "[…]" in text
    assert len(text) <= 300 + 20


def test_pick_passages_without_question_takes_the_start():
    text, cut = web.pick_passages("\n".join(f"para {i} " * 10 for i in range(50)), budget=500)
    assert cut and text.startswith("para 0") and "[…]" not in text


async def test_search_shows_sources_outside_the_model(ctx, monkeypatch):
    monkeypatch.setattr(web, "_search", lambda q, n: [
        {"title": f"Result {i}", "body": f"snippet {i}", "href": f"https://site{i}.example/"} for i in range(5)])
    out = await web.web_search({"query": "paper 1.21"}, ctx)
    assert "snippet 0" in out.text and "https://" not in out.text  # links go in the footer, not the prompt
    assert out.footer.count("https://") == 3 and "1. Result 0\nhttps://site0.example/" in out.footer


async def test_search_failure_is_friendly(ctx, monkeypatch):
    def boom(q, n):
        raise RuntimeError("ratelimit")
    monkeypatch.setattr(web, "_search", boom)
    assert "couldn't reach the web" in (await web.web_search({"query": "x"}, ctx)).text


async def test_agent_puts_footer_after_the_answer(ctx, store):
    reg = Registry()
    reg.add("web", web.tools(ctx, transport=site({"https://docs.example.com/a": (200, HTML, ARTICLE.encode())})))
    llm = FakeLLM("web", tool="read_url", args={"url": "https://docs.example.com/a"}, text="Use depends_on.")
    reply = await Agent(llm, store, reg, ctx).handle(1, "summarise https://docs.example.com/a")
    assert reply.text == "Use depends_on.\n\n🔗 https://docs.example.com/a"
