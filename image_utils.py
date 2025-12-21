"""
Image utility functions for SOMEWHERE game.
Handles panel extraction, image manipulation, and processing.
"""

from PIL import Image
from pathlib import Path
from typing import Optional

def extract_panel_from_grid(image_path: str, panel: int = 4) -> Optional[str]:
    """
    Extract a specific panel from a 2x2 grid flipbook image.
    
    Args:
        image_path: Path to the 4-panel grid image
        panel: Which panel to extract (1=top-left, 2=top-right, 3=bottom-left, 4=bottom-right)
    
    Returns:
        Path to the extracted panel image, or None if extraction fails
    """
    try:
        # Load the full grid image
        img = Image.open(image_path)
        width, height = img.size
        
        print(f"[FLIPBOOK] Extracting panel {panel} from {width}x{height} image")
        
        # Panel positions in 2x2 grid
        # (left, top, right, bottom)
        panels = {
            1: (0, 0, width//2, height//2),          # Top-left
            2: (width//2, 0, width, height//2),      # Top-right
            3: (0, height//2, width//2, height),     # Bottom-left
            4: (width//2, height//2, width, height)  # Bottom-right
        }
        
        if panel not in panels:
            print(f"[FLIPBOOK] ERROR: Invalid panel number {panel}, must be 1-4")
            return None
        
        # Crop the specified panel
        box = panels[panel]
        panel_img = img.crop(box)
        
        # Generate output path for extracted panel
        path = Path(image_path)
        canonical_path = path.parent / f"{path.stem}_panel{panel}{path.suffix}"
        
        # Save extracted panel
        panel_img.save(canonical_path)
        print(f"[FLIPBOOK] Saved panel {panel} to: {canonical_path}")
        
        return str(canonical_path)
        
    except Exception as e:
        print(f"[FLIPBOOK] ERROR extracting panel: {e}")
        import traceback
        traceback.print_exc()
        return None


def is_flipbook_grid(image_path: str) -> bool:
    """
    Attempt to detect if an image is a 2x2 flipbook grid.
    
    This is a heuristic check - looks for visual patterns that suggest
    a grid layout (like repeated similar compositions).
    
    Args:
        image_path: Path to image to analyze
    
    Returns:
        True if image appears to be a flipbook grid, False otherwise
    """
    try:
        img = Image.open(image_path)
        width, height = img.size
        
        # Basic heuristic: If aspect ratio is close to 1:1 (square-ish),
        # it's more likely to be a 2x2 grid than a standard 16:9 or 4:3 frame
        aspect_ratio = width / height
        
        # Square-ish images (0.9 to 1.1 ratio) are likely grids
        if 0.9 <= aspect_ratio <= 1.1:
            print(f"[FLIPBOOK] Image appears to be grid (aspect: {aspect_ratio:.2f})")
            return True
        
        # Very wide images (>1.5) are likely single frames
        if aspect_ratio > 1.5:
            print(f"[FLIPBOOK] Image appears to be single frame (aspect: {aspect_ratio:.2f})")
            return False
        
        # Uncertain - default to False (safer to treat as single frame)
        print(f"[FLIPBOOK] Uncertain if grid (aspect: {aspect_ratio:.2f}), treating as single frame")
        return False
        
    except Exception as e:
        print(f"[FLIPBOOK] ERROR checking if grid: {e}")
        return False


def validate_panel_extraction(original_path: str, panel_path: str) -> bool:
    """
    Validate that panel extraction worked correctly.
    
    Args:
        original_path: Path to original grid image
        panel_path: Path to extracted panel
    
    Returns:
        True if extraction valid, False otherwise
    """
    try:
        original = Image.open(original_path)
        panel = Image.open(panel_path)
        
        orig_w, orig_h = original.size
        panel_w, panel_h = panel.size
        
        # Panel should be roughly half the width and height of original
        expected_w = orig_w // 2
        expected_h = orig_h // 2
        
        # Allow 5% tolerance for rounding
        w_match = abs(panel_w - expected_w) / expected_w < 0.05
        h_match = abs(panel_h - expected_h) / expected_h < 0.05
        
        if w_match and h_match:
            print(f"[FLIPBOOK] Panel extraction validated: {panel_w}x{panel_h}")
            return True
        else:
            print(f"[FLIPBOOK] Panel size mismatch: expected ~{expected_w}x{expected_h}, got {panel_w}x{panel_h}")
            return False
            
    except Exception as e:
        print(f"[FLIPBOOK] ERROR validating extraction: {e}")
        return False

