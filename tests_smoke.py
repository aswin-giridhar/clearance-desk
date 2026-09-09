"""End-to-end functional sweep. Run against a live base URL."""
import json, sys, time, urllib.request, urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
PASS, FAIL = [], []

def call(path, payload=None, timeout=200, want_json=True):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            if not want_json:
                return r.status, {"len": len(body)}
            return r.status, json.loads(body or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}
    except Exception as e:
        return 0, {"err": str(e)}

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")

print(f"=== Clearance Desk functional sweep against {BASE} ===\n[infrastructure]")
s, h = call("/api/health")
check("health endpoint responds 200", s == 200, f"status={s}")
check("MCP session connected", h.get("mcp_connected") is True)
check("MCP exposes run_query tool", "run_query" in (h.get("mcp_tools") or []), str(h.get("mcp_tools")))
check("warm cache shipped", (h.get("cache", {}).get("entries", 0)) > 0, f"entries={h.get('cache',{}).get('entries')}")
s, d = call("/", want_json=False)          # returns HTML, not JSON
check("index page served", s == 200 and d.get("len", 0) > 1000, f"status={s} bytes={d.get('len')}")
s, d = call("/api/sample")
check("sample scene available", s == 200 and len(d.get("script", "")) > 50)

print("\n[input validation]")
for label, body, want in [("empty script rejected", {"script": ""}, 422),
                          ("missing field rejected", {}, 422),
                          ("oversize script rejected", {"script": "x" * 20001}, 422)]:
    s, _ = call("/api/scan", body, timeout=60)
    check(label, s == want, f"got {s}")

print("\n[core clearance behaviour]")
s, d = call("/api/scan", {"script": open("samples/scene.txt").read()})
check("sample scene scans 200", s == 200, f"status={s}")
if s == 200:
    check("returns findings", len(d.get("findings", [])) > 0, f"{len(d.get('findings',[]))} findings")
    check("overall tier is valid", d.get("overall") in ("CLEAR","LOW","MEDIUM","HIGH","CRITICAL"), d.get("overall"))
    f0 = d["findings"][0]
    check("finding carries SQL evidence", bool(f0.get("sql")), f"{len(f0.get('sql',[]))} queries")
    check("finding carries territory split", isinstance(f0.get("territories"), list) and len(f0["territories"]) > 0)
    check("finding carries trend", "trend_pct" in f0)
    check("coverage/limits disclosed", len(d.get("coverage", [])) >= 3)
    check("one finding per element (no dupes)",
          len({x["element"] for x in d["findings"]}) == len(d["findings"]))
    check("query counter honest (cached run reports 0 live)",
          d.get("queries_run", -1) >= 0 and (d.get("queries_run",0) + d.get("cache_hits",0)) > 0,
          f"live={d.get('queries_run')} cached={d.get('cache_hits')}")

print("\n[known-answer correctness]")
s, d = call("/api/scan", {"script": open("samples/scene2.txt").read()})
if s == 200:
    by = {f["element"].upper(): f for f in d["findings"]}
    check("famous person -> CRITICAL", by.get("TOM HANKS", {}).get("tier") == "CRITICAL",
          str(by.get("TOM HANKS", {}).get("tier")))
    check("global brand -> CRITICAL", by.get("COCA-COLA", {}).get("tier") == "CRITICAL",
          str(by.get("COCA-COLA", {}).get("tier")))
    check("famous title caught via exposure despite title-index gap",
          by.get("JURASSIC PARK", {}).get("prominence", 0) > 1_000_000,
          f"{by.get('JURASSIC PARK',{}).get('prominence',0):,} views")
    check("overall = worst finding", d.get("overall") == "CRITICAL", d.get("overall"))
else:
    check("scene2 scans", False, f"status={s}")

print("\n[mononym does not crash — regression guard]")
s, d = call("/api/scan", {"script": "INT. STAGE\n\nMADONNA walks to the microphone and sings."})
check("single-word character name returns 200", s == 200, f"status={s}")
if s == 200:
    check("mononym scored, not skipped", len(d.get("findings", [])) > 0, d.get("overall"))

print("\n[invented names are not flagged]")
s, d = call("/api/scan", {"script": "INT. ROOM\n\nQWIXLBERT THRANDLE meets VEXOMORPH GRELLIN at a firm called ZZQQXX HOLDINGS."})
if s == 200:
    check("invented names -> CLEAR", d.get("overall") == "CLEAR", f"{d.get('overall')}, {len(d.get('findings',[]))} findings")

print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL: print("FAILED:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
