"""
Simple inventory system for SOMEWHERE game.
Tracks items, detects pickups, manages inventory state.
"""

# Item Registry - Simple dictionary
ITEMS = {
    # Weapons
    "crowbar": {"type": "weapon", "display": "Crowbar", "emoji": "⚒️"},
    "knife": {"type": "weapon", "display": "Combat Knife", "emoji": "🔪"},
    "pipe": {"type": "weapon", "display": "Metal Pipe", "emoji": "🔧"},
    "gun": {"type": "weapon", "display": "Handgun", "emoji": "🔫"},
    "wrench": {"type": "weapon", "display": "Wrench", "emoji": "🔧"},
    "bat": {"type": "weapon", "display": "Baseball Bat", "emoji": "⚾"},
    
    # Tools
    "flashlight": {"type": "tool", "display": "Flashlight", "emoji": "🔦"},
    "radio": {"type": "tool", "display": "Radio", "emoji": "📻"},
    "medkit": {"type": "tool", "display": "Medical Kit", "emoji": "💊"},
    "bolt_cutters": {"type": "tool", "display": "Bolt Cutters", "emoji": "✂️"},
    "rope": {"type": "tool", "display": "Rope", "emoji": "🪢"},
    "keycard": {"type": "tool", "display": "Keycard", "emoji": "🔑"},
}

MAX_INVENTORY_SIZE = 5

# Pickup detection keywords
PICKUP_KEYWORDS = [
    "grab", "grab the", "grabbed", "grabbed the",
    "pick up", "pick up the", "picked up", "picked up the",
    "take", "take the", "took", "took the",
    "find", "find the", "found", "found the",
    "snatch", "snatch the", "snatched", "snatched the",
    "seize", "seize the", "seized", "seized the",
    "acquire", "acquire the", "acquired", "acquired the",
    "obtain", "obtain the", "obtained", "obtained the",
]

def detect_item_pickups(text: str, current_inventory: list) -> list:
    """
    Scan text for item pickups and return list of newly acquired items.
    
    Args:
        text: Dispatch or vision text to scan
        current_inventory: Current inventory list
    
    Returns:
        List of item IDs that were picked up
    """
    if not text:
        return []
    
    text_lower = text.lower()
    picked_up = []
    
    # Check each item in registry
    for item_id, item_data in ITEMS.items():
        # Skip if already in inventory
        if item_id in current_inventory:
            continue
        
        item_name = item_data["display"].lower()
        
        # Check for pickup keywords + item name
        for keyword in PICKUP_KEYWORDS:
            pattern = f"{keyword} {item_name}"
            if pattern in text_lower:
                picked_up.append(item_id)
                print(f"[INVENTORY] Detected pickup: {item_id} (pattern: '{pattern}')")
                break
        
        # Also check for just item name after certain verbs
        if not picked_up or item_id not in picked_up:
            # Simple check: "find crowbar", "grab knife", etc.
            for keyword in ["grab", "pick up", "take", "find", "snatch"]:
                if f"{keyword} {item_name}" in text_lower:
                    if item_id not in picked_up:
                        picked_up.append(item_id)
                        print(f"[INVENTORY] Detected pickup: {item_id} (simple pattern)")
                        break
    
    return picked_up

def add_items_to_inventory(current_inventory: list, new_items: list) -> tuple[list, list]:
    """
    Add items to inventory, respecting max size.
    
    Args:
        current_inventory: Current inventory list
        new_items: List of item IDs to add
    
    Returns:
        (updated_inventory, items_that_didnt_fit)
    """
    updated = current_inventory.copy()
    didnt_fit = []
    
    for item in new_items:
        if len(updated) < MAX_INVENTORY_SIZE:
            updated.append(item)
        else:
            didnt_fit.append(item)
    
    return updated, didnt_fit

def remove_item_from_inventory(current_inventory: list, item_id: str) -> list:
    """Remove item from inventory and return updated list."""
    updated = current_inventory.copy()
    if item_id in updated:
        updated.remove(item_id)
    return updated

def format_inventory_display(inventory: list) -> str:
    """
    Format inventory for Discord display.
    
    Returns formatted string like:
    ⚒️  Crowbar
    🔦 Flashlight
    🔪 Combat Knife
    """
    if not inventory:
        return "*Empty*"
    
    lines = []
    for item_id in inventory:
        item = ITEMS.get(item_id)
        if item:
            lines.append(f"{item['emoji']} {item['display']}")
    
    return "\n".join(lines)

def get_inventory_summary(inventory: list) -> str:
    """Get one-line summary like 'Crowbar, Flashlight, Knife'"""
    if not inventory:
        return "Empty"
    
    names = [ITEMS[item_id]["display"] for item_id in inventory if item_id in ITEMS]
    return ", ".join(names)

def should_consume_item(dispatch_text: str, item_id: str) -> bool:
    """
    Determine if item should be consumed based on how it was used in dispatch.
    
    Returns True if item was heavily used and should be consumed.
    """
    if not dispatch_text:
        return False
    
    text_lower = dispatch_text.lower()
    item_name = ITEMS.get(item_id, {}).get("display", "").lower()
    
    # Check for consumption indicators
    consumption_keywords = [
        "break", "broke", "broken", "shatter", "shattered",
        "snap", "snapped", "crack", "cracked",
        "lose", "lost", "drop", "dropped", "fall", "fell",
        "wear out", "worn out", "exhausted", "depleted",
        "use up", "used up", "consume", "consumed"
    ]
    
    # Check if item name appears near consumption keywords
    for keyword in consumption_keywords:
        # Look for patterns like "the crowbar breaks" or "you break the crowbar"
        if keyword in text_lower and item_name in text_lower:
            # Rough proximity check (within 50 chars)
            keyword_pos = text_lower.find(keyword)
            item_pos = text_lower.find(item_name)
            if abs(keyword_pos - item_pos) < 50:
                return True
    
    # Random chance for heavy usage (50% when item is explicitly used)
    if item_name in text_lower:
        import random
        return random.random() < 0.5
    
    return False

def extract_item_from_choice(choice_text: str) -> str:
    """
    Extract item ID from choice text like "Pry open door [Crowbar]"
    
    Returns item_id or None
    """
    import re
    match = re.search(r'\[([^\]]+)\]', choice_text)
    if match:
        item_display_name = match.group(1).strip()
        # Find item by display name
        for item_id, item_data in ITEMS.items():
            if item_data["display"].lower() == item_display_name.lower():
                return item_id
    return None

