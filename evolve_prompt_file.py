import json
import openai
import os
import re

# --- Load Config --- # (supports Render deployment with env vars)
try:
    with open("config.json", encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

# --- OpenAI API Key --- #
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", config.get("OPENAI_API_KEY"))

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY

# --- Load narrative prompts --- #
with open("prompts/simulation_prompts.json", "r", encoding="utf-8") as f:
    PROMPTS = json.load(f)

# --- Helper to get latest dispatch image path --- #
def get_latest_dispatch_image(state_file="world_state.json"):
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        img_path = state.get("dispatch_image")
        if img_path:
            return "http://127.0.0.1:8000" + img_path
    except Exception as e:
        print(f"❌ Failed to get dispatch image: {e}")
    return None

# --- Helper to condense history for prompt injection --- #
def condense_history(dispatches):
    # If more than 5, summarize earlier ones (placeholder for real summarization)
    if len(dispatches) > 5:
        # TODO: Implement actual summarization logic for dispatches[:-3]
        return dispatches[-5:]
    return dispatches

def build_evolution_prompt(current_world_prompt, dispatches, latest_phase, choice, state_file="world_state.json"):
    """Create a prompt to evolve the world based on phase and recent dispatches, now focused on Jason Fleece's perspective and local changes only."""
    # Defensive: ensure dispatches is a list of dicts
    if not isinstance(dispatches, list):
        print("⚠️ dispatches is not a list! Value:", dispatches)
        dispatches = []
    # Read last_character and seen_elements from state file
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        seen_elements = state.get("seen_elements", [])
        current_beat_idx = state.get("current_beat", 0)
    except Exception as e:
        print(f"❌ Failed to read or parse {state_file} for seen_elements: {e}")
        seen_elements = []
        current_beat_idx = 0
    # Load story beats for beat nudge
    try:
        beats = PROMPTS["story_progression_phases"]
        beat = beats[current_beat_idx] if 0 <= current_beat_idx < len(beats) else beats[0]
        beat_nudge = f"Current story beat: {beat.split('•')[0].strip()}. Focus: {beat.split('•')[1].strip()}"
    except Exception as e:
        print(f"❌ Failed to load story beats for beat nudge: {e}")
        beat_nudge = ""
    # Only use the last 5 dispatches (or fewer)
    trimmed_dispatches = condense_history(dispatches)
    dispatch_snippets = "\n".join(
        f"- {d['dispatch']}" for d in trimmed_dispatches if isinstance(d, dict) and 'dispatch' in d
    )
    # Use the world_evolution_instructions from PROMPTS
    prompt_body = PROMPTS["world_evolution_instructions"] + (
        f"\nCORE CONTEXT:\n{current_world_prompt}\nRECENT DISPATCHES:\n{dispatch_snippets}\nCURRENT PHASE: {latest_phase}\n{beat_nudge}\nPLAYER CHOICE:\n{choice or '(no choice this turn)'}\n"
    )
    return prompt_body

# --- Evolve World State --- #
def evolve_world_state(dispatches, consequence_summary=None, state_file="world_state.json", vision_description=None):
    """Evolve the world prompt based on dispatch history, phase, and consequence summary."""
    # Defensive: ensure dispatches is a list
    if not isinstance(dispatches, list):
        print("⚠️ dispatches is not a list! Value:", dispatches)
        dispatches = []
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print(f"❌ Failed to read {state_file}: {e}")
        return False

    # --- TURN COUNTER --- #
    turn_count = state.get("turn_count", 0) + 1
    state["turn_count"] = turn_count

    old_world = state.get("world_prompt", "")
    core_context = state.get("core_context", "")
    last_choice = state.get("last_choice", "")
    seen_elements = state.get("seen_elements", [])
    current_phase = state.get("current_phase", "normal")

    # --- WORLD EVENT NUDGE --- #
    world_event_nudge = ""
    # Phase-based interval for world event nudges
    if current_phase == "normal":
        interval = 4
        nudge_text = "(It has been quiet for a while; perhaps Jason notices a subtle sign of outside activity—distant noise, graffiti, or a new patrol.)"
    elif current_phase == "escalating":
        interval = 2
        nudge_text = "(Tension is rising; consider a moderate world intrusion—an unexpected noise, a new face, or a sign of faction activity.)"
    else:  # critical
        interval = 1
        nudge_text = "(The world is in chaos; frequent, major events or new actors may intrude on Jason's story.)"
    if turn_count % interval == 0:
        world_event_nudge = nudge_text

    # --- PROMPT MODIFICATION --- #
    def build_evolution_prompt_with_intrusions(current_world_prompt, dispatches, latest_phase, choice, state_file="world_state.json", vision_description=None):
        """Enhanced prompt to allow subtle world intrusions and phase-based frequency."""
        # Get location context from vision description
        location_context = ""
        if vision_description:
            location_context = f"JASON'S CURRENT VIEW: {vision_description[:200]}\n\n"
            # Determine if inside or outside based on vision
            vision_lower = vision_description.lower()
            if any(word in vision_lower for word in ["hallway", "corridor", "room", "interior", "ceiling", "wall", "door", "inside"]):
                environment_type = "INSIDE A FACILITY"
            else:
                environment_type = "OUTSIDE"
        else:
            location_context = f"CURRENT LOCATION: {current_world_prompt}\n\n"
            environment_type = "UNKNOWN LOCATION"
        
        # Use simple world evolution instructions from JSON
        prompt_body = PROMPTS["world_evolution_instructions"]
        
        # Add location context
        prompt_body += f"\n\nCURRENT LOCATION/SCENE: {vision_description if vision_description else current_world_prompt}"
        prompt_body += f"\n\nLAST ACTION: {choice}"
        prompt_body += "\n\nGenerate ONE sentence atmospheric change that STAYS IN THE CURRENT ENVIRONMENT."
        
        if vision_description:
            prompt_body += f"\n\nVISION ANALYSIS: {vision_description}"
        return prompt_body

    # Use the enhanced prompt builder
    prompt = build_evolution_prompt_with_intrusions(old_world, dispatches, current_phase, last_choice, state_file, vision_description=vision_description)
    if consequence_summary:
        prompt += f"\n\nIMPORTANT CONSEQUENCE OF LAST ACTION:\n- {consequence_summary}\n"

    # --- Vision model feedback: include up to 2 previous dispatch images if available --- #
    image_urls = []
    for entry in reversed(dispatches):
        if "image" in entry and entry["image"] and len(image_urls) < 2:
            url = entry["image"]
            if not url.startswith("http"):  # If not already a full URL
                url = "http://127.0.0.1:8000" + url
            image_urls.append(url)
        if len(image_urls) >= 2:
            break
    image_urls = list(reversed(image_urls))  # Oldest first

    # Only include image URLs that are public (not localhost or 127.0.0.1)
    public_image_urls = [url for url in image_urls if not ("127.0.0.1" in url or "localhost" in url)]

    if public_image_urls:
        print(f"Including {len(public_image_urls)} dispatch images in LLM context: {public_image_urls}")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                *[{"type": "image_url", "image_url": {"url": url}} for url in public_image_urls]
            ]
        }]
    else:
        messages = [{"role": "user", "content": prompt}]

    try:
        # Request the world evolution from Gemini Flash (much faster!)
        import requests
        gemini_api_key = config.get("GEMINI_API_KEY", "")
        
        # Convert messages to Gemini format
        prompt_text = ""
        for msg in messages:
            if msg["role"] == "system":
                prompt_text += f"{msg['content']}\n\n"
            elif msg["role"] == "user":
                prompt_text += msg["content"]
        
        print("[GEMINI TEXT] Calling Gemini 2.0 Flash for world evolution...")
        response_data = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {"temperature": 0.8, "maxOutputTokens": 60}  # Force brevity!
            },
            timeout=30
        ).json()
        print("[GEMINI TEXT] ✅ World evolution complete")
        
        # Extract text from Gemini response
        new_world_prompt = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()

        print(f"[WORLD EVOLUTION] OLD:\n{old_world}\n---\nNEW:\n{new_world_prompt}\n")
        # Save to persistent log
        log_path = os.path.join("logs", "world_evolution.log")
        os.makedirs("logs", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"[WORLD EVOLUTION]\nPROMPT:\n{prompt}\n\nDISPATCHES:\n{json.dumps(dispatches[-12:], ensure_ascii=False, indent=2)}\n\nOLD:\n{old_world}\n---\nNEW:\n{new_world_prompt}\n\n")

        if not new_world_prompt or new_world_prompt == old_world:
            print("⚠️ No meaningful change in world_prompt — skipping update.")
            # Nudge chaos_level upward if nothing new happens
            state["chaos_level"] = int(state.get("chaos_level", 0)) + 1
            # TODO: Penalize repeat dispatches (e.g., check for repeated phrases and rewrite)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            return False

        # Update the world state
        state["world_prompt"] = new_world_prompt

        # --- Update seen_elements with new unique elements from the new world prompt --- #
        # Simple placeholder: split on commas and periods, add new phrases not already in seen_elements
        new_elements = [e.strip() for e in re.split(r'[.,\n]', new_world_prompt) if len(e.strip()) > 3]
        for elem in new_elements:
            if elem and elem not in seen_elements:
                seen_elements.append(elem)
        state["seen_elements"] = seen_elements

        # Save the updated world state to the file
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        return True
    except Exception as e:
        print(f"❌ OpenAI evolution failed: {e}")
        return False

def generate_scene_hook(state_file="world_state.json", prompt_file="prompts/simulation_prompts.json"):
    """Generate a one-line cinematic hook for the current beat and save it to world_state.json."""
    # Load state and prompt config
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt_config = json.load(f)
    beats = prompt_config["story_progression_phases"]
    beat_idx = state.get("current_beat", 0)
    beat = beats[beat_idx] if 0 <= beat_idx < len(beats) else beats[0]
    base = state.get("world_prompt", "")
    chaos = state.get("chaos_level", 0)
    hook_prompt = f"""
Beat: {beat}
World state: {base} (chaos {chaos})
Write a single, cinematic sentence describing the next shot.
"""
    # Use Gemini Flash for speed
    import requests
    gemini_api_key = config.get("GEMINI_API_KEY", "")
    
    response_data = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": hook_prompt}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 80}
        },
        timeout=10
    ).json()
    
    class GeminiResponse:
        def __init__(self, text):
            self.choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': text})()})]
    
    gemini_text = response_data["candidates"][0]["content"]["parts"][0]["text"]
    response = GeminiResponse(gemini_text)
    hook = response.choices[0].message.content.strip()
    state["scene_hook"] = hook
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    print(f"[SCENE HOOK] {hook}")

def set_current_beat(state, beat_idx, prompt_file="prompts/simulation_prompts.json"):
    """Set the current beat and map chaos_level accordingly."""
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt_config = json.load(f)
    beats = prompt_config["story_progression_phases"]
    state["current_beat"] = beat_idx
    # Map chaos to beat
    low  = ["Establish", "Aftermath"]
    mid  = ["Explore", "Discover"]
    high = ["Escalate", "Climax"]
    name = beats[beat_idx].split("•")[0].strip()
    if name in low:
        state["chaos_level"] = 0.2
    elif name in mid:
        state["chaos_level"] = 0.5
    else:
        state["chaos_level"] = 0.9
    return state

def get_last_image_path(state_file="world_state.json"):
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Try dispatch_image, world_image, or other relevant keys
        for key in ("dispatch_image", "world_image", "image"):
            img_path = state.get(key)
            if img_path:
                return img_path
    except Exception as e:
        print(f"❌ Failed to get last image: {e}")
    return None

def describe_image_with_vision(image_path):
    if not image_path:
        return None
    from pathlib import Path
    import openai
    img_path = Path(image_path.lstrip("/"))
    if not img_path.exists():
        img_path = Path("images") / img_path.name
    if not img_path.exists():
        print(f"[Vision] Image not found: {img_path}")
        return None
    try:
        # Use Gemini vision via engine._ask with image_path parameter
        from engine import _ask
        img_path_str = str(img_path)
        if img_path_str.startswith("images/"):
            img_path_str = "/" + img_path_str
        elif not img_path_str.startswith("/"):
            img_path_str = "/images/" + img_path.name
        
        prompt = "Describe briefly what is happening in this image. Be short and descriptive. Do NOT use poetic or flowery language. Include actions and verbs taking place. include color and time of day."
        result = _ask(prompt, model="gemini", temp=0.3, tokens=200, image_path=img_path_str)
        
        # Create a mock response object for compatibility
        class MockResp:
            def __init__(self, text):
                self.choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': text})()})]
        
        rsp = MockResp(result)
        desc = rsp.choices[0].message.content.strip().replace("\n", " ")
        return desc
    except Exception as e:
        print(f"[Vision] Error describing image: {e}")
        return None

def generate_interim_messages(prompt_file="prompts/simulation_prompts.json", n=20, world_state=None, choice=None, image_desc=None):
    """
    Call the LLM to generate n interim messages using the prompt template,
    context‑aware of the world state, last choice, and last image description, then save them to the prompt_file.
    """
    import json, openai

    # 1. load template
    with open(prompt_file, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    template = cfg["loading_message_instructions"]

    # 2. inject context
    ctx = ""
    if world_state:
        ctx += f"\nWorld: {world_state}"
    if choice:
        ctx += f"\nChoice: {choice}"
    if image_desc:
        ctx += f"\nLast Image: {image_desc}"
    # 2.5. harden prompt
    harden = "\nBe short and descriptive. Do NOT use poetic or flowery language."

    # 3. ask LLM for fresh lines (use Gemini Flash)
    import requests
    gemini_api_key = config.get("GEMINI_API_KEY", "")
    
    response_data = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": llm_prompt}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 200}
        },
        timeout=10
    ).json()
    
    class GeminiResp:
        def __init__(self, text):
            self.choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': text})()})]
    
    gemini_text = response_data["candidates"][0]["content"]["parts"][0]["text"]
    resp = GeminiResp(gemini_text)
    
    lines = [l.strip() for l in resp.choices[0].message.content.splitlines() if l.strip()][:n]

    # 4. overwrite bank
    cfg["interim_messages"] = lines
    with open(prompt_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    print(f"[INTERIM MESSAGES] Generated {len(lines)} lines based on choice={choice!r} image_desc={image_desc!r}")


def generate_interim_messages_on_demand(n=20, prompt_file="prompts/simulation_prompts.json", world_state=None, choice=None, dispatch_prompt_style=None, image_desc=None):
    """Generate a list of interim messages using the LLM and return them as a list (do not write to file)."""
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt_config = json.load(f)
    interim_prompt = prompt_config["loading_message_instructions"]
    context = ""
    if world_state:
        context += f"\nCurrent world state: {world_state}"
    if choice:
        context += f"\nCurrent choice: {choice}"
    if dispatch_prompt_style:
        context += f"\nDispatch prompt style: {dispatch_prompt_style}"
    if image_desc:
        context += f"\nLast Image: {image_desc}"
    # Add explicit examples and a uniqueness nudge to the prompt
    example = (
        '\n\nEXAMPLES OF THE EXPECTED RETURN:\n'
        '[\n'
        '  "Static on all channels.",\n'
        '  "Red dust thickens.",\n'
        '  "Camera offline.",\n'
        '  "Footsteps echo.",\n'
        '  "Lights flicker.",\n'
        '  "Breathing is difficult.",\n'
        '  "Broken badge found.",\n'
        '  "Something moves.",\n'
        '  "No response.",\n'
        '  "Metallic clang.",\n'
        '  "Map missing.",\n'
        '  "Torn photo.",\n'
        '  "Static grows louder.",\n'
        '  "Door ajar.",\n'
        '  "Footprints in dust.",\n'
        '  "Lens cracked.",\n'
        '  "Faint voice, then silence.",\n'
        '  "Red glow pulses.",\n'
        '  "Shadows shift.",\n'
        '  "Cold air rushes in."\n'
        ']\n'
        'Return a JSON array of exactly 20 unique, context-aware, analog-horror style lines, as shown above. Each line must be unique and not repeat the wording or content of the others. Do not return fewer than 20 lines. Do not return an empty array.'
    )
    harden = "\nBe short and descriptive. Do NOT use poetic or flowery language."
    llm_prompt = f"{interim_prompt}{context}{example}{harden}"
    # Use Gemini Flash for speed
    import requests
    gemini_api_key = config.get("GEMINI_API_KEY", "")
    
    response_data = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": llm_prompt}]}],
            "generationConfig": {"temperature": 0.8, "maxOutputTokens": 512}
        },
        timeout=10
    ).json()
    
    content = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    # Always split into lines, stripping bullets and whitespace
    interim_lines = [l.strip('-•* ') for l in content.splitlines() if l.strip()]
    return interim_lines[:n]

def summarize_world_prompt_to_interim_messages(world_prompt: str):
    """Summarize the world prompt into 2 short, varied-length (1-3 sentences) in-universe embedded texts (e.g., radio log, field note, intercepted message), with no 'Log' prefix or numbering. Each should be clipped, atmospheric, and concise. Return only a JSON array of 2 strings."""
    import openai
    import json as pyjson
    prompt = (
        "Summarize the following world update into exactly 2 short, in-universe embedded texts (e.g., radio log, field note, intercepted message). Each should be 1-3 sentences, clipped, atmospheric, and concise. Do not include any 'Log' prefix or numbering. Return ONLY a JSON array of 2 strings.\n\nWORLD UPDATE:\n" + world_prompt + "\n"
    )
    # Use Gemini Flash for speed
    import requests
    gemini_api_key = config.get("GEMINI_API_KEY", "")
    
    response_data = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        headers={"x-goog-api-key": gemini_api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 120}
        },
        timeout=10
    ).json()
    
    class GeminiResp:
        def __init__(self, text):
            self.choices = [type('obj', (object,), {'message': type('obj', (object,), {'content': text})()})]
    
    gemini_text = response_data["candidates"][0]["content"]["parts"][0]["text"]
    response = GeminiResp(gemini_text)
    
    content = response.choices[0].message.content.strip()
    # Try to parse as JSON array
    try:
        start = content.find("[")
        end = content.rfind("]") + 1
        arr = pyjson.loads(content[start:end])
        if isinstance(arr, list) and len(arr) == 2 and all(isinstance(x, str) and x.strip() for x in arr):
            return [x.strip() for x in arr]
    except Exception:
        pass
    # Fallback: extract first 2 non-trivial, non-duplicate lines
    lines = [l.strip('-•* ",') for l in content.splitlines() if l.strip()]
    filtered = [l for l in lines if not any(x in l.lower() for x in ["sorry", "guideline", "here they are", "json", "can provide", "```", "explanation", "preamble"])]
    unique = []
    for l in filtered:
        if l and l not in unique:
            unique.append(l)
    while len(unique) < 2:
        unique.append("The world holds its breath.")
    return unique[:2]
