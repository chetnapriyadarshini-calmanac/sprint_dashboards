"""
retro_layout.py
===============
Shared HTML shell (page head + CSS + footer) for the retro dashboards.
Format PAGE_HEAD with {title} and {meta}; append sections; close with PAGE_FOOT.
"""

PAGE_HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b;min-height:100vh}}
.hdr{{background:#1e293b;border-bottom:1px solid #334155;padding:20px 28px;color:#fff}}
.hdr h1{{font-size:22px;font-weight:800}}
.hdr .meta{{font-size:12px;color:#94a3b8;margin-top:4px}}
.wrap{{max-width:1280px;margin:0 auto;padding:24px 28px}}
.section{{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.section h2{{font-size:15px;font-weight:700;color:#1e293b;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px}}
.card .val{{font-size:24px;font-weight:800}}
.card .lbl{{font-size:11px;color:#64748b;margin-top:3px}}
.card .sub{{font-size:10px;color:#94a3b8;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{padding:8px 10px;text-align:left;color:#64748b;font-size:11px;border-bottom:1px solid #e2e8f0;background:#f8fafc}}
td{{padding:7px 10px;border-bottom:1px solid #f1f5f9;vertical-align:middle}}
.badge{{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;display:inline-block}}
</style></head><body>
<div class="hdr">
  <h1>🔄 {title}</h1>
  <div class="meta">{meta}</div>
</div>
<div class="wrap">
"""

PAGE_FOOT = """</div></body></html>
"""
