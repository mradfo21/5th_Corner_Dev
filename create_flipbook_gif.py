"""
Convert a 4x4 grid image into an animated GIF flipbook.

Takes a 4x4 grid (16 panels) and creates a smooth animated GIF
that plays through all frames in sequence.
"""

from PIL import Image
from pathlib import Path
import os


def extract_panels_from_grid(grid_image_path: Path, output_dir: Path = None) -> list[Path]:
    """
    Extract 16 individual panels from a 4x4 grid image.
    
    Args:
        grid_image_path: Path to the 4x4 grid image
        output_dir: Optional directory to save individual panels (for debugging)
    
    Returns:
        List of 16 PIL Image objects in sequence (top-left to bottom-right)
    """
    try:
        grid = Image.open(grid_image_path)
        width, height = grid.size
        
        # Validate it's evenly divisible by 4
        if width % 4 != 0 or height % 4 != 0:
            print(f"[FLIPBOOK GIF ERROR] Image dimensions not evenly divisible by 4: {width}x{height}")
            return []
        
        panel_width = width // 4
        panel_height = height // 4
        
        print(f"[FLIPBOOK GIF] Grid size: {width}x{height}")
        print(f"[FLIPBOOK GIF] Panel size: {panel_width}x{panel_height}")
        
        panels = []
        
        # Extract panels in reading order (left-to-right, top-to-bottom)
        for row in range(4):
            for col in range(4):
                x = col * panel_width
                y = row * panel_height
                
                # Crop the panel
                panel = grid.crop((x, y, x + panel_width, y + panel_height))
                panels.append(panel)
                
                # Optionally save individual panels for debugging
                if output_dir:
                    panel_num = row * 4 + col + 1
                    panel_path = output_dir / f"panel_{panel_num:02d}.png"
                    panel.save(panel_path)
                    print(f"[FLIPBOOK GIF] Saved panel {panel_num}: {panel_path}")
        
        print(f"[FLIPBOOK GIF] Extracted {len(panels)} panels")
        return panels
    
    except Exception as e:
        print(f"[FLIPBOOK GIF ERROR] Failed to extract panels: {e}")
        import traceback
        traceback.print_exc()
        return []


def create_animated_gif(panels: list, output_path: Path, duration_ms: int = 500, loop: int = 0) -> Path:
    """
    Create an animated GIF from a sequence of PIL Image objects.
    
    Args:
        panels: List of PIL Image objects (frames)
        output_path: Path to save the output GIF
        duration_ms: Duration of each frame in milliseconds (default: 500ms = 8s total for 16 frames)
        loop: Number of times to loop (0 = infinite, 1 = play once, 2+ = loop N times)
    
    Returns:
        Path to the created GIF
    """
    try:
        if not panels:
            print(f"[FLIPBOOK GIF ERROR] No panels provided")
            return None
        
        print(f"[FLIPBOOK GIF] Creating animated GIF with {len(panels)} frames")
        print(f"[FLIPBOOK GIF] Frame duration: {duration_ms}ms")
        print(f"[FLIPBOOK GIF] Total duration: {duration_ms * len(panels)}ms ({duration_ms * len(panels) / 1000:.1f}s)")
        print(f"[FLIPBOOK GIF] Loop: {'infinite' if loop == 0 else f'{loop} time(s)'}")
        
        # Save as animated GIF
        panels[0].save(
            output_path,
            save_all=True,
            append_images=panels[1:],
            duration=duration_ms,
            loop=loop,
            optimize=False  # Keep quality high, don't optimize
        )
        
        print(f"[FLIPBOOK GIF] [OK] Created animated GIF: {output_path}")
        print(f"[FLIPBOOK GIF] File size: {os.path.getsize(output_path) / 1024:.1f} KB")
        
        return output_path
    
    except Exception as e:
        print(f"[FLIPBOOK GIF ERROR] Failed to create GIF: {e}")
        import traceback
        traceback.print_exc()
        return None


def grid_to_flipbook_gif(grid_image_path: Path, output_gif_path: Path = None, duration_ms: int = 500, loop: int = 0, save_panels: bool = False) -> dict:
    """
    Convert a 4x4 grid image into an animated GIF flipbook.
    
    Args:
        grid_image_path: Path to the 4x4 grid image
        output_gif_path: Optional path for output GIF (defaults to same name with .gif extension)
        duration_ms: Duration of each frame in milliseconds
        loop: Number of times to loop (0 = infinite)
        save_panels: If True, save individual panel images for debugging
    
    Returns:
        Dictionary with:
            'gif_path': Path to the created GIF, or None if failed
            'first_frame': Path to the first frame panel (if saved)
            'last_frame': Path to the last frame panel (if saved)
    """
    print(f"[FLIPBOOK GIF] Converting grid to animated GIF")
    print(f"[FLIPBOOK GIF] Input: {grid_image_path}")
    
    # Default output path
    if output_gif_path is None:
        output_gif_path = grid_image_path.parent / f"{grid_image_path.stem}_flipbook.gif"
    
    # We ALWAYS save first and last panels now for temporal consistency
    panels_dir = grid_image_path.parent / f"{grid_image_path.stem}_panels"
    panels_dir.mkdir(exist_ok=True)
    
    # Extract panels
    panels = extract_panels_from_grid(grid_image_path, output_dir=panels_dir)
    
    if not panels:
        return {'gif_path': None, 'first_frame': None, 'last_frame': None}
    
    # Create animated GIF
    gif_path = create_animated_gif(panels, output_gif_path, duration_ms=duration_ms, loop=loop)
    
    # Determine first and last frame paths
    first_frame_path = panels_dir / "panel_01.png"
    last_frame_path = panels_dir / "panel_16.png"
    
    # Cleanup intermediate panels if save_panels is False, but KEEP 01 and 16
    if not save_panels:
        for i in range(2, 16):
            try:
                panel_file = panels_dir / f"panel_{i:02d}.png"
                if panel_file.exists():
                    os.remove(panel_file)
            except:
                pass
    
    return {
        'gif_path': gif_path,
        'first_frame': first_frame_path,
        'last_frame': last_frame_path
    }


# ─────────────────────────────────────────────────────────────────────────────
# TEST / CLI USAGE
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python create_flipbook_gif.py <grid_image_path> [duration_ms] [loop]")
        print("Example: python create_flipbook_gif.py flipbook_grid.png 250 0")
        print("         python create_flipbook_gif.py flipbook_grid.png 200 1  # Play once, faster")
        sys.exit(1)
    
    grid_path = Path(sys.argv[1])
    duration = int(sys.argv[2]) if len(sys.argv) > 3 else 500
    loop_count = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    
    if not grid_path.exists():
        print(f"ERROR: File not found: {grid_path}")
        sys.exit(1)
    
    print("="*70)
    print("FLIPBOOK GIF GENERATOR")
    print("="*70)
    
    gif_path = grid_to_flipbook_gif(
        grid_path,
        duration_ms=duration,
        loop=loop_count,
        save_panels=True  # Save panels for inspection
    )
    
    if gif_path:
        print("="*70)
        print(f"[SUCCESS] Animated GIF created:")
        print(f"  {gif_path}")
        print("="*70)
    else:
        print("="*70)
        print("[FAILED] Could not create animated GIF")
        print("="*70)
        sys.exit(1)

