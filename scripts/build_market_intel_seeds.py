"""Konversi dataset publik mentah -> CSV seed MCP-V1-02.

Sumber & lisensi: mcp_marketing/services/market_intel/data/README.md. Unduh file mentah ke folder
terpisah (data tak tepercaya), lalu jalankan dengan `-I`:
    python -I scripts/build_market_intel_seeds.py <wilayah_level_1_2.sql> <code2020.csv> \
        <category2020.csv> <ISIC_Rev_4_english_structure.Txt> \
        mcp_marketing/services/market_intel/data
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path

wil_sql, kbli_codes, kbli_cats, isic_txt, out = map(Path, sys.argv[1:6])
out.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- regions
ROW = re.compile(
    r"^\('(\d{2}(?:\.\d{2})?)','([^']*(?:''[^']*)*)','([^']*(?:''[^']*)*)',\s*(-?[\d.]+|NULL),\s*(-?[\d.]+|NULL),"
)
regions = []
for line in wil_sql.read_text(encoding="utf-8").splitlines():
    m = ROW.match(line)
    if not m:
        continue
    code, name, capital, lat, lng = m.groups()
    name, capital = name.replace("''", "'"), capital.replace("''", "'")
    level = "province" if len(code) == 2 else "regency_city"
    regions.append(
        {
            "code": code,
            "level": level,
            "parent_code": code[:2] if level == "regency_city" else "",
            "name": name,
            "capital": capital,
            "centroid_lat": f"{float(lat):.6f}" if lat != "NULL" else "",
            "centroid_lng": f"{float(lng):.6f}" if lng != "NULL" else "",
        }
    )
LNG_FIXES = {"74.07": 123.538901}  # Wakatobi: sumber menulis 23.538901 (digit '1' hilang)
for r in regions:
    if r["code"] in LNG_FIXES:
        r["centroid_lng"] = f"{LNG_FIXES[r['code']]:.6f}"
    r["name"] = r["name"].strip()
with (out / "regions.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(regions[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(regions)
prov = sum(r["level"] == "province" for r in regions)
print(f"regions: {prov} provinsi, {len(regions) - prov} kab/kota")

# ---------------------------------------------------------------- ISIC
isic = {}
division_section = {}
section = None
with isic_txt.open(encoding="utf-8") as f:
    for row in csv.DictReader(f):
        code, desc = row["Code"].strip(), row["Description"].strip()
        isic[code] = desc
        if code.isalpha():
            section = code
        elif len(code) == 2:
            division_section[code] = section

# ---------------------------------------------------------------- KBLI
LEVEL = {1: "section", 2: "division", 3: "group", 4: "class", 5: "subclass"}
rows = []
with kbli_cats.open(encoding="utf-8") as f:
    for r in csv.DictReader(f):
        rows.append(
            {
                "kbli_code": r["code"].strip(),
                "parent_code": "",
                "level": "section",
                "name_id": r["name"].strip(),
                "name_en": isic.get(r["code"].strip(), ""),
                "description": " ".join(r["description"].split()),
            }
        )
with kbli_codes.open(encoding="utf-8") as f:
    for r in csv.DictReader(f):
        code = r["code"].strip()
        if not code.isdigit() or not 2 <= len(code) <= 5:
            raise SystemExit(f"kode KBLI aneh: {code!r}")
        parent = division_section.get(code) if len(code) == 2 else code[:-1]
        if parent is None:
            raise SystemExit(f"divisi tanpa section ISIC: {code}")
        rows.append(
            {
                "kbli_code": code,
                "parent_code": parent,
                "level": LEVEL[len(code)],
                "name_id": " ".join(r["title"].split()),
                "name_en": isic.get(code, "") if len(code) <= 4 else "",
                "description": " ".join((r["description"] or "").split()),
            }
        )
MISSING_GROUPS = {  # golongan yang tidak ada di sumber; judul mengikuti KBLI 2020 (ditambah manual)
    "151": "INDUSTRI KULIT DAN BARANG DARI KULIT, TERMASUK KULIT BUATAN",
    "331": "REPARASI PRODUK LOGAM PABRIKASI, MESIN DAN PERALATAN",
    "511": "ANGKUTAN UDARA PENUMPANG",
    "512": "ANGKUTAN UDARA BARANG",
    "662": "AKTIVITAS PENUNJANG ASURANSI DAN DANA PENSIUN",
    "872": "AKTIVITAS SOSIAL DI DALAM PANTI UNTUK DISABILITAS MENTAL, MASALAH KESEHATAN JIWA "
    "DAN PENYALAHGUNAAN ZAT ADIKTIF",
}
present = {r["kbli_code"] for r in rows}
for code, title in MISSING_GROUPS.items():
    if code not in present:
        rows.append(
            {
                "kbli_code": code,
                "parent_code": code[:2],
                "level": "group",
                "name_id": title,
                "name_en": isic.get(code, ""),
                "description": "",
            }
        )
rows.sort(key=lambda r: (r["level"] != "section", r["kbli_code"]))
codes = {r["kbli_code"] for r in rows}
orphans = [r["kbli_code"] for r in rows if r["parent_code"] and r["parent_code"] not in codes]
if orphans:
    print("PERINGATAN parent hilang:", orphans[:20], len(orphans))
with (out / "kbli2020.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
print("kbli:", {lvl: sum(r["level"] == lvl for r in rows) for lvl in LEVEL.values()})

# ------------------------------------------- KBLI -> ISIC (diturunkan dari struktur)
mapping = []
for r in rows:
    code = r["kbli_code"]
    if r["level"] == "section":
        cands = [(code, "exact")]
    else:
        prefix = code[:4]
        cands = [
            (prefix[:n], "exact" if n == len(code) else lvl)
            for n, lvl in ((4, "class"), (3, "group"), (2, "division"))
            if n <= len(code)
        ]
    for isic_code, match in cands:
        if isic_code in isic:
            mapping.append(
                {
                    "kbli_code": code,
                    "isic_code": isic_code,
                    "isic_name": isic[isic_code],
                    "match_level": "exact" if len(isic_code) == len(code) else match,
                }
            )
            break
    else:
        print("tanpa ISIC:", code)
with (out / "kbli2020_isic4.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(mapping[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(mapping)
print("isic map:", len(mapping), Counter(m["match_level"] for m in mapping))
