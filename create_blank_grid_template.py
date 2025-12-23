"""
Create a blank 4x4 grid template for flipbook layout reference.
This template shows ONLY the grid structure with NO content.
"""

from PIL import Image, ImageDraw
from pathlib import Path

def create_blank_grid_template(output_path: Path, grid_size: tuple = (1200, 896), line_width: int = 4):
    """
    Create a blank 4x4 grid template with visible gridlines.
    
    Args:
        output_path: Where to save the template
        grid_size: Total size of the grid (width, height)
        line_width: Width of the grid lines in pixels
    """
    width, height = grid_size
    
    # Create a dark gray/black background (VHS aesthetic)
    img = Image.new('RGB', (width, height), color=(30, 30, 35))
    draw = ImageDraw.Draw(img)
    
    # Calculate panel dimensions
    panel_width = width // 4
    panel_height = height // 4
    
    # Draw vertical lines (3 lines to create 4 columns)
    for i in range(1, 4):
        x = i * panel_width
        draw.line([(x, 0), (x, height)], fill=(80, 80, 90), width=line_width)
    
    # Draw horizontal lines (3 lines to create 4 rows)
    for i in range(1, 4):
        y = i * panel_height
        draw.line([(0, y), (width, y)], fill=(80, 80, 90), width=line_width)
    
    # Save template
    img.save(output_path)
    print(f"[TEMPLATE] Created blank 4x4 grid template: {output_path}")
    print(f"[TEMPLATE] Grid size: {width}x{height}, Panel size: {panel_width}x{panel_height}")
    
    return output_path


if __name__ == "__main__":
    # Create the template in prompts directory
    output = Path("prompts") / "flipbook_blank_grid_template.png"
    output.parent.mkdir(exist_ok=True)
    create_blank_grid_template(output)

