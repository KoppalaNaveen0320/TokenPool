import re

with open('full-ui.html', 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

# Let's inspect the first page-card: page-01-landing
p1_idx = text.find('id="page-01-landing"')
if p1_idx != -1:
    p2_idx = text.find('id="page-02-login"', p1_idx)
    chunk = text[p1_idx:p2_idx if p2_idx != -1 else p1_idx+5000]
    print(f"Page 1 chunk length: {len(chunk)}")
    print("Page 1 snippet:\n", chunk[:2000])

# Let's see what is inside the page-card: is it an iframe, an embedded html, an image, or DOM elements?
data_imgs = re.findall(r'<img[^>]+src="(data:image/[^"]+)"', text)
print(f"Total img data URIs: {len(data_imgs)}")
if data_imgs:
    print(f"First image data URI prefix: {data_imgs[0][:100]}... length: {len(data_imgs[0])}")
