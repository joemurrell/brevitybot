import os
import re
import csv
from bs4 import BeautifulSoup

# ---- Hardcoded directory with exported HTML pages ----
HTML_DIR = r"C:\Users\joemu\Downloads\brevity"
OUTPUT_CSV = os.path.join(HTML_DIR, "brevity_terms.csv")

# ---- Known brevity keys ----
brevity_keys_order = ['*', '**', '[A/A]', '[AIR-MAR]', '[A/S]', '[EW]', '[MAR]', '[S/A]', '[SO]', '[S/S]']
brevity_key_regex = re.compile(r'\[(A/A|AIR-MAR|A/S|EW|MAR|S/A|SO|S/S)\]')

def extract_ordered_keys(text):
    keys = []
    if text.startswith("**"):
        keys.append("**")
    elif text.startswith("*"):
        keys.append("*")
    keys += [f"[{k}]" for k in brevity_key_regex.findall(text)]
    seen = set()
    return " ".join([k for k in brevity_keys_order if k in keys and not (k in seen or seen.add(k))])

def extract_keys_and_clean(text):
    # Extract asterisks at the start
    keys = []
    asterisk_match = re.match(r'^(\*\*|\*)', text.strip())
    if asterisk_match:
        keys.append(asterisk_match.group(0))
    # Extract brevity keys (e.g., [A/A])
    brevity_key_pattern = re.compile(r'\[(A/A|A/S|S/A|EW|AIR-MAR|MAR|SO|S/S)\]')
    keys += brevity_key_pattern.findall(text)
    # Remove asterisks and brevity keys from text, but keep placeholders like [location]
    text_clean = re.sub(r'^(\*\*|\*)', '', text).strip()
    text_clean = brevity_key_pattern.sub('', text_clean)
    text_clean = re.sub(r'\s+', ' ', text_clean).strip()
    return keys, text_clean

def split_definitions(definition):
    # Split on numbered definitions (e.g., 1. ... 2. ...), but keep the number as part of the definition
    parts = re.split(r'(?<=\.)\s*(?=\d+\.)', definition)
    return [p.strip() for p in parts if p.strip()]

def clean_brackets(text):
    # Remove newlines, fix unmatched brackets, and normalize spaces
    text = text.replace('\n', ' ').replace('\r', ' ')
    # Remove extra spaces
    text = re.sub(r'\s+', ' ', text)
    # Remove unmatched brackets at start/end
    text = re.sub(r'^\[([^\]]*)$', r'\1', text)
    text = re.sub(r'^([^\[]*)\]$', r'\1', text)
    return text.strip()

def clean_fields(term, definition):
    # Split definitions if needed
    def_parts = split_definitions(definition)
    rows = []
    for def_part in def_parts:
        # Extract keys from both term and this definition part
        term_keys, term_clean = extract_keys_and_clean(term)
        def_keys, def_clean = extract_keys_and_clean(def_part)
        # Combine keys, preserving order and removing duplicates
        seen = set()
        all_keys = []
        for k in term_keys + def_keys:
            k_fmt = k if '*' in k else f'[{k}]'
            if k_fmt not in seen:
                all_keys.append(k_fmt)
                seen.add(k_fmt)
        keys_str = ' '.join(all_keys)
        # Clean up brackets and line breaks
        term_final = clean_brackets(term_clean)
        def_final = clean_brackets(def_clean)
        keys_final = clean_brackets(keys_str)
        rows.append([term_final, def_final, keys_final])
    return rows



def parse_html_file(filepath):
    from bs4 import BeautifulSoup

    with open(filepath, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    tags = soup.find_all("p", style=True)
    blocks = []

    for tag in tags:
        style = tag["style"]
        top = int(re.search(r"top:(\d+)px", style).group(1))
        left = int(re.search(r"left:(\d+)px", style).group(1))
        text = tag.get_text(strip=True).replace("\xa0", " ")
        blocks.append((top, left, text))

    blocks.sort()

    terms = []
    current_term_parts = []
    current_def_parts = []
    in_definition = False

    for _, left, text in blocks:
        if left < 300:
            if in_definition:
                # Save previous entry before starting a new one
                term = " ".join(current_term_parts).strip()
                definition = " ".join(current_def_parts).strip()
                if term and definition:
                    terms.append((term, definition))
                current_term_parts = []
                current_def_parts = []
                in_definition = False
            current_term_parts.append(text)
        else:
            in_definition = True
            current_def_parts.append(text)

    # Add final entry
    if current_term_parts and current_def_parts:
        term = " ".join(current_term_parts).strip()
        definition = " ".join(current_def_parts).strip()
        terms.append((term, definition))

    return terms



def clean_field(field):
    # Remove leading/trailing quotes and replace double quotes with single
    field = field.strip().replace('"', '')
    return field

def main():
    all_entries = []

    for filename in sorted(os.listdir(HTML_DIR)):
        if filename.endswith(".html"):
            filepath = os.path.join(HTML_DIR, filename)
            terms = parse_html_file(filepath)
            for term, definition in terms:
                for row in clean_fields(term, definition):
                    row = [clean_field(x) for x in row]
                    all_entries.append(row)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Term", "Definition", "Keys"])
        writer.writerows(all_entries)

    print(f"✅ Parsed {len(all_entries)} terms into {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
