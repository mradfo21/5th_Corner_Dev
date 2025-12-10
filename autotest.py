import engine
import random
import json
import os
import sys
import base64
import time
import openai
import argparse

# Define and ensure autotest directory exists at the very top
AUTOTEST_DIR = "autotest"
os.makedirs(AUTOTEST_DIR, exist_ok=True)

# At the very top, after defining AUTOTEST_DIR
DONE_MARKER = os.path.join(AUTOTEST_DIR, "autotest_done.txt")
if os.path.exists(DONE_MARKER):
    os.remove(DONE_MARKER)

# Clear output files at the start of each run
for fname in [
    "autotest_run.json",
    "autotest_conceptual_ideas.json",
    "autotest_suggestions.json",
    "autotest_suggestions_raw.json",
    "autotest_image_analysis.json",
    "autotest_highlevel_suggestions.json"
]:
    fpath = os.path.join(AUTOTEST_DIR, fname)
    if os.path.exists(fpath):
        os.remove(fpath)

CONFIG_PATH = os.path.join(AUTOTEST_DIR, "autotest_config.json")

# Load config
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

focus_areas = config.get("focus", ["story", "images", "choices"])
run_params = config.get("run_params", {})
prompts = config.get("prompts", {})
auto_plan = config.get("auto_plan", True)

# Helper to get a prompt from config or raise error
def get_prompt(key):
    if key not in prompts:
        raise RuntimeError(f"Missing required prompt '{key}' in config.")
    return prompts[key]

def list_codebase_files():
    # List the files included in the codebase context
    files = [
        "choices.py",
        "evolve_prompt_file.py",
        "prompts/simulation_prompts.json",
        "engine.py"
    ]
    return files

# --- Self-improvement analysis (requires openai package and API key) ---
def analyze_conceptual_ideas(log_path=None, image_mode=False):
    """
    Analyze the playthrough log and suggest high-level conceptual improvements using GPT (openai API required).
    Returns a list of conceptual ideas.
    """
    if log_path is None:
        log_path = os.path.join(AUTOTEST_DIR, "autotest_run.json")
    try:
        import openai
    except ImportError:
        print("[WARN] openai package not installed. Skipping analysis.")
        return ["[openai not installed]"]
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        try:
            api_key = engine.CONFIG["OPENAI_API_KEY"]
        except Exception:
            api_key = None
    if not api_key:
        print("[WARN] OPENAI_API_KEY not set and not found in config. Skipping analysis.")
        return ["[OPENAI_API_KEY not set]"]
    openai.api_key = api_key
    with open(log_path, "r", encoding="utf-8") as f:
        history = f.read()
    test_context = get_prompt("test_context").format(image_mode=image_mode)
    analysis_prompt = get_prompt("analysis_prompt").format(history=history)
    prompt = test_context + "\n" + analysis_prompt
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500
    )
    ideas_text = response.choices[0].message.content
    # Strip markdown code block markers if present
    if ideas_text.strip().startswith('```json'):
        ideas_text = ideas_text.strip()[7:]
    if ideas_text.strip().startswith('```'):
        ideas_text = ideas_text.strip()[3:]
    if ideas_text.strip().endswith('```'):
        ideas_text = ideas_text.strip()[:-3]
    try:
        ideas = json.loads(ideas_text)
        with open(os.path.join(AUTOTEST_DIR, "autotest_conceptual_ideas.json"), "w", encoding="utf-8") as f:
            json.dump(ideas, f, indent=2)
        print("Conceptual ideas saved to autotest/autotest_conceptual_ideas.json", flush=True)
    except Exception as e:
        print("Failed to parse conceptual ideas as JSON, saving raw output for debugging:", e)
        with open(os.path.join(AUTOTEST_DIR, "autotest_conceptual_ideas.json"), "w", encoding="utf-8") as f:
            f.write(ideas_text)
        print("Raw conceptual ideas saved to autotest/autotest_conceptual_ideas.json", flush=True)
        ideas = [ideas_text]
    return ideas

# --- Headless test runner ---
def run_headless_test(turns=10, image_mode=False):
    """
    Run a headless, automated playthrough of the game for testing.
    If image_mode is False, disables image generation for speed.
    Returns the full playthrough log as a list of dicts.
    """
    # Optionally disable image generation
    if not image_mode:
        engine.IMAGE_ENABLED = False
        engine.WORLD_IMAGE_ENABLED = False
    else:
        engine.IMAGE_ENABLED = True
        engine.WORLD_IMAGE_ENABLED = True

    # Start a new game
    engine.generate_intro_turn()
    history = []
    for i in range(turns):
        snap = engine.begin_tick()
        choices = snap.get("choices", [])
        # Use a fallback if no valid choices
        valid_choices = [c for c in choices if c and c != "—"]
        if not valid_choices:
            print(f"No valid choices at turn {i+1}, using fallback choice.")
            choice = "Wait"
        else:
            choice = random.choice(valid_choices)
        print(f"Turn {i+1}: Choosing -> {choice}")
        result = engine.complete_tick(choice)
        history.append({
            "turn": i+1,
            "choice": choice,
            "dispatch": result.get("dispatch"),
            "image": result.get("dispatch_image"),
            "caption": result.get("caption"),
            "choices": result.get("choices", [])
        })
        # Optionally print/log
        print("Dispatch:", result.get("dispatch"))
        if image_mode:
            print("Image:", result.get("dispatch_image"))
    # Save the run for later analysis
    with open(os.path.join(AUTOTEST_DIR, "autotest_run.json"), "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    print(f"Test complete. {len(history)} turns logged to autotest/autotest_run.json.")
    # Wait for all images to be generated if image_mode is enabled
    if image_mode:
        expected_images = turns
        actual_images = sum(1 for h in history if h.get('image'))
        retries = 0
        while actual_images < expected_images and retries < 10:
            print(f"[WARN] Only {actual_images}/{expected_images} images generated. Waiting for all images before analysis...")
            time.sleep(2)
            with open(os.path.join(AUTOTEST_DIR, "autotest_run.json"), "r", encoding="utf-8") as f:
                history = json.load(f)
            actual_images = sum(1 for h in history if h.get('image'))
            retries += 1
        if actual_images < expected_images:
            print(f"[WARN] Proceeding with {actual_images}/{expected_images} images after waiting.")
    print("All turns complete. Starting analysis...")
    if image_mode:
        analyze_images_with_gpt4v(history, image_dir=os.path.join(AUTOTEST_DIR, "images"))
    return history

# --- Self-improvement loop scaffold ---
def self_improvement_loop(iterations=3, turns=4, image_mode=False):
    """
    Run several playthroughs, analyze each, and print AI suggestions for improvement.
    """
    for i in range(iterations):
        print(f"\n=== Self-Improvement Iteration {i+1} ===")
        run_headless_test(turns=turns, image_mode=image_mode)
        conceptual_ideas = analyze_conceptual_ideas(image_mode=image_mode)
        print("\nAI Conceptual Ideas:\n", conceptual_ideas)

def analyze_images_with_gpt4v(history, image_dir=None):
    if image_dir is None:
        image_dir = os.path.join(AUTOTEST_DIR, "images")
    """
    Analyze all images in the playthrough using GPT-4 Vision, comparing each to previous images for continuity and consistency.
    Saves the results to autotest_image_analysis.json.
    """
    import traceback
    try:
        from openai import OpenAI
    except ImportError:
        print("[WARN] openai package not installed. Skipping image analysis.")
        return
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        try:
            api_key = engine.CONFIG["OPENAI_API_KEY"]
        except Exception:
            api_key = None
    if not api_key:
        print("[WARN] OPENAI_API_KEY not set and not found in config. Skipping image analysis.")
        return
    client = OpenAI(api_key=api_key)
    # Collect all images and their context
    image_contexts = []
    for entry in history:
        img_path = entry.get("image")
        if img_path:
            # Handle relative paths (e.g., /images/filename.png)
            if not os.path.exists(img_path):
                img_candidate = img_path.lstrip("/")
                if not img_candidate.startswith("images/"):
                    img_candidate = os.path.join("images", os.path.basename(img_candidate))
                if os.path.exists(img_candidate):
                    img_path = img_candidate
            if os.path.exists(img_path):
                with open(img_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
                image_contexts.append({
                    "image_b64": img_b64,
                    "caption": entry.get("caption", ""),
                    "dispatch": entry.get("dispatch", ""),
                    "turn": entry.get("turn", 0)
                })
    print(f"[DEBUG] Found {len(image_contexts)} images for analysis.")
    for i, ctx in enumerate(image_contexts):
        print(f"[DEBUG] Image {i+1} base64 size: {len(ctx['image_b64'])}")
    if not image_contexts:
        print("[INFO] No images found for analysis.")
        return
    prompt = (
        "You are an analog horror narrative QA assistant. Here is a sequence of images generated for a story, along with their captions and narrative context. For each image: "
        "- Describe what is actually visible. "
        "- Compare it to the previous image(s) for spatial and narrative continuity. "
        "- Note any inconsistencies, unexplained changes, or visual errors. "
        "- Suggest improvements to the image prompt or narrative to improve visual continuity and immersion. "
        "\n\nHere are the images and their contexts, in order."
    )
    input_list = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt}
            ]
        }
    ]
    for i, ctx in enumerate(image_contexts):
        input_list.append({
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"Turn {ctx['turn']}:\nCaption: {ctx['caption']}\nDispatch: {ctx['dispatch']}"},
                {"type": "input_image", "image_url": f"data:image/png;base64,{ctx['image_b64']}"}
            ]
        })
    print(f"[DEBUG] Input to OpenAI Vision API: {json.dumps(input_list, indent=2)[:1000]} ...")
    try:
        response = client.responses.create(
            model="gpt-4o",
            input=input_list
        )
        try:
            analysis = response.output[0].content[0].text
            print(f"[DEBUG] Analysis text: {analysis}")
        except Exception as e:
            print(f"[ERROR] Failed to extract analysis text: {e}")
            print(f"[DEBUG] response.output: {getattr(response, 'output', None)}")
            if hasattr(response, 'output') and len(response.output) > 0:
                print(f"[DEBUG] response.output[0]: {response.output[0]}")
                if hasattr(response.output[0], 'content') and len(response.output[0].content) > 0:
                    print(f"[DEBUG] response.output[0].content[0]: {response.output[0].content[0]}")
            analysis = str(response)
        with open(os.path.join(AUTOTEST_DIR, "autotest_image_analysis.json"), "w", encoding="utf-8") as f:
            f.write(analysis)
        print("[INFO] Image analysis saved to autotest/autotest_image_analysis.json.")
        return analysis
    except Exception as e:
        print(f"[ERROR] GPT-4 Vision API call failed: {e}")
        traceback.print_exc()
        return None

# --- Choice/dispatch coherence analysis (text-only) ---
def analyze_choice_coherence(history_path=None):
    """
    For each turn, check if the choices are logically and contextually grounded in the dispatch.
    Save results to autotest/autotest_choice_coherence.json.
    """
    if history_path is None:
        history_path = os.path.join(AUTOTEST_DIR, "autotest_run.json")
    with open(history_path, "r", encoding="utf-8") as f:
        history = json.load(f)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        try:
            api_key = engine.CONFIG["OPENAI_API_KEY"]
        except Exception:
            api_key = None
    if not api_key:
        print("[WARN] OPENAI_API_KEY not set and not found in config. Skipping choice coherence analysis.")
        return
    openai.api_key = api_key
    prompt = (
        "You are a narrative QA assistant for an analog horror interactive fiction game. "
        "For each turn, you will be given the dispatch (scene description) and the list of choices presented to the player. "
        "For each turn, answer the following:\n"
        "- Are all choices logically and contextually grounded in the dispatch?\n"
        "- Flag any choices that are incoherent, vague, or unrelated to the dispatch.\n"
        "- Suggest a more grounded or specific alternative for any flagged choice.\n"
        "Format your answer as a JSON array, one object per turn, with fields: turn, dispatch, choices, issues (list), suggestions (list).\n"
        "Here is the playthrough log:\n" + json.dumps([
            {"turn": h["turn"], "dispatch": h["dispatch"], "choices": h["choices"]} for h in history
        ], indent=2)
    )
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1200
    )
    analysis = response.choices[0].message.content
    # Strip markdown code block markers if present
    if analysis.strip().startswith('```json'):
        analysis = analysis.strip()[7:]
    if analysis.strip().startswith('```'):
        analysis = analysis.strip()[3:]
    if analysis.strip().endswith('```'):
        analysis = analysis.strip()[:-3]
    try:
        parsed = json.loads(analysis)
        with open(os.path.join(AUTOTEST_DIR, "autotest_choice_coherence.json"), "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2)
        print("Choice/dispatch coherence saved to autotest/autotest_choice_coherence.json", flush=True)
    except Exception as e:
        print("Failed to parse choice coherence as JSON, saving raw output for debugging:", e)
        with open(os.path.join(AUTOTEST_DIR, "autotest_choice_coherence.json"), "w", encoding="utf-8") as f:
            f.write(analysis)
        print("Raw choice coherence saved to autotest/autotest_choice_coherence.json", flush=True)
    return analysis

if __name__ == "__main__":
    # Parse command-line arguments for max turns override
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-turns', type=int, default=None, help='Override the number of turns for autotest run')
    args, unknown = parser.parse_known_args()
    # Always pull run parameters from config, error if missing
    try:
        turns = run_params["turns"]
        iterations = run_params["iterations"]
        image_mode = run_params["image_mode"]
    except KeyError as e:
        raise RuntimeError(f"Missing required run parameter in config: {e}")
    # Apply --max-turns override if provided
    if args.max_turns is not None:
        print(f"[INFO] Overriding turns: {turns} -> {args.max_turns}")
        turns = args.max_turns
    for i in range(iterations):
        print(f"\n=== Autotest Iteration {i+1} ===")
        history = run_headless_test(turns=turns, image_mode=image_mode)
        # Optionally analyze images
        image_analysis = None
        if image_mode:
            image_analysis = analyze_images_with_gpt4v(history, image_dir=os.path.join(AUTOTEST_DIR, "images"))
        # Analyze choice/dispatch coherence (text-only)
        analyze_choice_coherence()
        # Collect all feedback
        with open(os.path.join(AUTOTEST_DIR, "autotest_run.json"), "r", encoding="utf-8") as f:
            playthrough_log = f.read()
        image_analysis_text = ""
        if image_analysis:
            image_analysis_text = image_analysis
        # Prompt LLM for high-level, creative suggestions for focus areas
        try:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                try:
                    api_key = engine.CONFIG["OPENAI_API_KEY"]
                except Exception:
                    api_key = None
            if api_key:
                openai.api_key = api_key
                focus_str = ", ".join(focus_areas)
                feedback_prompt = get_prompt("feedback_prompt").format(
                    focus_str=focus_str,
                    playthrough_log=playthrough_log,
                    image_analysis_text=image_analysis_text
                )
                response = openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": feedback_prompt}],
                    max_tokens=800
                )
                suggestions = response.choices[0].message.content
                # Save to autotest/autotest_highlevel_suggestions.json
                with open(os.path.join(AUTOTEST_DIR, "autotest_highlevel_suggestions.json"), "w", encoding="utf-8") as f:
                    f.write(suggestions)
                print("High-level suggestions saved to autotest/autotest_highlevel_suggestions.json")
                # --- AUTO PLAN LOGIC ---
                if auto_plan:
                    # Use a new prompt for the action plan
                    plan_prompt = prompts.get("plan_prompt")
                    if not plan_prompt:
                        raise RuntimeError("Missing required prompt 'plan_prompt' in config.")
                    plan_prompt_filled = plan_prompt.format(suggestions=suggestions, playthrough_log=playthrough_log, image_analysis_text=image_analysis_text)
                    plan_response = openai.chat.completions.create(
                        model="gpt-4o",
                        messages=[{"role": "user", "content": plan_prompt_filled}],
                        max_tokens=600
                    )
                    plan_text = plan_response.choices[0].message.content
                    with open(os.path.join(AUTOTEST_DIR, "autotest_action_plan.json"), "w", encoding="utf-8") as f:
                        f.write(plan_text)
                    print("Action plan saved to autotest/autotest_action_plan.json\nSummary:\n", plan_text)
            else:
                print("[WARN] OPENAI_API_KEY not set. Skipping high-level suggestions and action plan.")
        except Exception as e:
            print(f"[ERROR] Failed to get high-level suggestions or action plan: {e}")
    print("Autotest complete.")
    # Write completion marker file
    with open(DONE_MARKER, "w") as f:
        f.write("done\n") 