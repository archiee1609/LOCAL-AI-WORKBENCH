import os
from pathlib import Path
from PIL import Image, ImageDraw

icons_dir = Path("frontend/public/icons")
icons_dir.mkdir(parents=True, exist_ok=True)

# Generate SVG
svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#6366f1" />
      <stop offset="50%" stop-color="#4f46e5" />
      <stop offset="100%" stop-color="#312e81" />
    </linearGradient>
    <linearGradient id="glow" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#a5b4fc" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#000" flood-opacity="0.35" />
    </filter>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#bg)" />
  <g filter="url(#shadow)" transform="translate(256, 256)">
    <path d="M0 -140 L35 -35 L140 0 L35 35 L0 140 L-35 35 L-140 0 L-35 -35 Z" fill="url(#glow)" />
    <circle cx="0" cy="0" r="28" fill="#ffffff" />
    <circle cx="100" cy="-100" r="16" fill="#38bdf8" opacity="0.9" />
    <circle cx="-110" cy="90" r="12" fill="#818cf8" opacity="0.8" />
  </g>
</svg>"""
(icons_dir / "icon.svg").write_text(svg_content, encoding="utf-8")

def create_png_icon(size, is_maskable=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    radius = int(size * 0.22) if not is_maskable else 0
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=(79, 70, 229, 255))
    
    center = size // 2
    accent_r = int(size * 0.28)
    
    points = [
        (center, center - accent_r),
        (center + int(accent_r * 0.25), center - int(accent_r * 0.25)),
        (center + accent_r, center),
        (center + int(accent_r * 0.25), center + int(accent_r * 0.25)),
        (center, center + accent_r),
        (center - int(accent_r * 0.25), center + int(accent_r * 0.25)),
        (center - accent_r, center),
        (center - int(accent_r * 0.25), center - int(accent_r * 0.25))
    ]
    draw.polygon(points, fill=(165, 180, 252, 255))
    
    core_r = int(size * 0.06)
    draw.ellipse([center - core_r, center - core_r, center + core_r, center + core_r], fill=(255, 255, 255, 255))
    
    s1 = int(size * 0.035)
    draw.ellipse([center + int(accent_r * 0.7) - s1, center - int(accent_r * 0.7) - s1, 
                  center + int(accent_r * 0.7) + s1, center - int(accent_r * 0.7) + s1], fill=(56, 189, 248, 240))
    
    return img

create_png_icon(192).save(icons_dir / "icon-192.png", "PNG")
create_png_icon(512).save(icons_dir / "icon-512.png", "PNG")
create_png_icon(512, is_maskable=True).save(icons_dir / "icon-maskable-512.png", "PNG")
print("PWA icons generated successfully!")
