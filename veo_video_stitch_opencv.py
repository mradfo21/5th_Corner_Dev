# Replacement for stitch_video_segments using OpenCV instead of ffmpeg

import cv2
from pathlib import Path
from typing import Optional, Tuple, List
from datetime import datetime

def stitch_video_segments_opencv(segment_paths: List[str], output_path: Path) -> Tuple[Optional[str], str]:
    """
    Stitch multiple video segments into a single video file using OpenCV.
    
    Args:
        segment_paths: List of paths to video segment files (in order)
        output_path: Path for output file
    
    Returns:
        Tuple of (output_path or None, error_message or empty string)
    """
    if not segment_paths:
        return None, "No video segments provided"
    
    # Verify all segment files exist
    missing = []
    for seg_path in segment_paths:
        if not Path(seg_path).exists():
            missing.append(seg_path)
    
    if missing:
        return None, f"Missing video segments: {', '.join(missing)}"
    
    print(f"[VIDEO STITCH] Stitching {len(segment_paths)} segments using OpenCV...")
    
    try:
        # Read first video to get properties
        first_cap = cv2.VideoCapture(str(segment_paths[0]))
        if not first_cap.isOpened():
            return None, f"Could not open first video: {segment_paths[0]}"
        
        fps = int(first_cap.get(cv2.CAP_PROP_FPS))
        width = int(first_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(first_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        first_cap.release()
        
        print(f"[VIDEO STITCH] Output: {width}x{height} @ {fps}fps")
        
        # Create video writer
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
        
        if not out.isOpened():
            return None, "Could not create output video writer"
        
        # Process each segment
        total_frames = 0
        for i, seg_path in enumerate(segment_paths):
            print(f"[VIDEO STITCH] Processing segment {i+1}/{len(segment_paths)}: {Path(seg_path).name}")
            
            cap = cv2.VideoCapture(str(seg_path))
            if not cap.isOpened():
                print(f"[VIDEO STITCH] Warning: Could not open {seg_path}, skipping")
                continue
            
            frames_written = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                
                # Resize frame if dimensions don't match
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))
                
                out.write(frame)
                frames_written += 1
                total_frames += 1
            
            cap.release()
            print(f"[VIDEO STITCH]   Wrote {frames_written} frames")
        
        # Release output writer
        out.release()
        
        if output_path.exists() and total_frames > 0:
            duration = total_frames / fps
            file_size_mb = output_path.stat().st_size / 1024 / 1024
            print(f"[VIDEO STITCH] Success! Stitched video: {output_path.name}")
            print(f"[VIDEO STITCH] Total: {total_frames} frames, {duration:.1f}s, {file_size_mb:.1f} MB")
            return str(output_path), ""
        else:
            return None, "No frames written to output video"
    
    except Exception as e:
        print(f"[VIDEO STITCH] Error: {e}")
        import traceback
        traceback.print_exc()
        safe_error = str(e).encode('ascii', 'replace').decode('ascii')
        return None, f"Stitching error: {safe_error}"


