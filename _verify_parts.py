"""Post-run verification: per-bank counts, junk-name patterns, empty-address ratio."""
import glob
import json
import re

JUNK = re.compile(r"holding no|agent banking|agent outlet|\batm\b|booth|offer|"
                  r"lounge|find branches|book an appointment|we are on the map|"
                  r"branch list|nearest branch|\bcrm\b|\bcdm\b|merchant", re.I)
prev = {"brac": (178, 32), "trust": (3, 0), "southeast": (0, 0),
        "mutual": (121, 37), "one_bank": (126, 54), "community": (26, 6),
        "bcbl": (75, 33), "city": (138, 80), "nrbc": (150, 541)}

def rec_name(e):
    return e.get("name") or list(e)[0]

def rec_addr(e):
    nm = rec_name(e)
    v = e.get(nm) if nm in e else None
    a = e.get("address")
    if not a:
        if isinstance(v, list) and v:
            a = v[0]
        elif isinstance(v, dict):
            a = v.get("address")
    return a if isinstance(a, str) else ("".join(a) if isinstance(a, list) else "")

tot_b = tot_s = 0
for p in sorted(glob.glob("output/parts/*.json")):
    d = json.load(open(p, encoding="utf-8"))
    br, sb = d.get("branch", []), d.get("sub_branch", [])
    tot_b += len(br)
    tot_s += len(sb)
    junk = [rec_name(e) for e in br + sb if JUNK.search(str(rec_name(e)))]
    noaddr = sum(1 for e in br + sb if not rec_addr(e).strip())
    tot = len(br) + len(sb)
    flag = ""
    key = next((k for k in prev if k in p), None)
    if key and tot != sum(prev[key]):
        flag = f"  <-- was {prev[key]}"
    if junk or (tot and noaddr / tot > 0.2):
        flag += f"  JUNK={junk[:3]} noaddr={noaddr}/{tot}"
    if d.get("status") != "success":
        flag += f"  STATUS={d.get('status')}"
    print(f"{p.split(chr(92))[-1]:42s} {len(br):5d} +{len(sb):4d}  "
          f"{str(d.get('method'))[:40]:42s}{flag}")
print(f"\nTOTAL: {tot_b} branches + {tot_s} subs across "
      f"{len(glob.glob('output/parts/*.json'))} banks")

