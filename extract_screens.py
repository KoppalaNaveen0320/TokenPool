import re
import os
import base64

os.makedirs('extracted_screens', exist_ok=True)

with open('full-ui.html', 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

# Let's inspect page-card tags and everything inside them
cards = re.findall(r'<div class="page-card" id="([^"]+)">\s*<div class="page-card-header">\s*<span class="page-num">([^<]+)</span>\s*<span class="page-label">([^<]+)</span>\s*</div>\s*<div class="page-thumb-wrap" style="([^"]*)"[^>]*>\s*<img class="page-thumb" src="data:image/png;base64,([^"]+)"', text)

print(f"Total cards matched with regex: {len(cards)}")

metadata = []
for pid, pnum, plabel, pstyle, pimg_b64 in cards:
    img_filename = f"extracted_screens/{pnum}_{pid}.png"
    # save png
    img_bytes = base64.b64decode(pimg_b64)
    with open(img_filename, 'wb') as img_f:
        img_f.write(img_bytes)
    
    metadata.append({
        'id': pid,
        'num': pnum,
        'label': plabel,
        'style': pstyle,
        'image': img_filename,
        'size_kb': len(img_bytes) // 1024
    })
    print(f"Saved {img_filename} ({len(img_bytes)//1024} KB) - {plabel}")

print(f"Successfully extracted {len(metadata)} screens.")
