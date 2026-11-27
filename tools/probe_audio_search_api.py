"""Real, token-free AUDIO search probe against the deployed read-only backend."""
import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-novel-search"
ORIGIN = "https://hub-google.github.io"


def query(params, origin=ORIGIN):
    req = urllib.request.Request(BASE + "?" + urllib.parse.urlencode(params),
                                 headers={"Origin": origin, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=65) as response:
            return response.status, dict(response.headers), json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.load(exc)


def main():
    status, headers, result = query({"q": "詭秘之主", "source": "shuba69"})
    assert status == 200, (status, result)
    assert headers.get("Access-Control-Allow-Origin") == ORIGIN
    assert result.get("keyword") == "詭秘之主", result
    assert result.get("sources", [])[0].get("source_id") == "shuba69", result
    books = result.get("results") or []
    assert any("69shuba.com/book/" in b["catalog_url"] for b in books), result
    print("Search returned", len(books), "69shuba candidates")

    status, _, result = query({"q": "詭秘之主"})
    assert status == 200, (status, result)
    assert len(result.get("sources") or []) == 9, result
    print("All nine sources searched. Candidate counts:",
          [(s["source_id"], len(s["results"]), s["status"]) for s in result["sources"]])

    status, _, actual = query({"q": "萬古第一廢"})
    assert status == 200, (status, actual)
    print("萬古第一廢 search results:", [(n["source_id"], n["title"]) for n in actual.get("results", [])])

    status, _, _ = query({"q": "詭秘之主"}, "https://untrusted.example")
    assert status == 403, status
    status, _, _ = query({"q": "詭秘之主", "source": "not-a-source"})
    assert status == 400, status
    print("Public backend, CORS and input validation verified")


if __name__ == "__main__":
    main()
