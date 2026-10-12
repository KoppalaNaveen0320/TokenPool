with open('full-ui.html', 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

import re

# Look for lightbox or any script or data structures in full-ui.html
scripts = re.findall(r'<script[^>]*>(.*?)</script>', text, re.DOTALL)
print(f"Total script tags: {len(scripts)}")
for i, sc in enumerate(scripts):
    print(f"Script {i+1} length: {len(sc)}")
    print(sc[:500])
