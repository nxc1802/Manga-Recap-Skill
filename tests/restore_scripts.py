import os
import csv
import sys
import subprocess
from pathlib import Path

def restore_chapter_script(csv_path: Path, chapter_num: str, project_title: str):
    print(f"Restoring script for chapter {chapter_num} from {csv_path}...")
    
    # Read rows from script.csv matching the chapter
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["chapter"].strip() == chapter_num:
                rows.append(row)
                
    if not rows:
        print(f"Warning: No rows found for chapter {chapter_num} in script.csv")
        return
        
    # Format as text script
    script_lines = []
    for r in rows:
        seq_id = r["sequence_id"].strip()
        text = r["text"].strip()
        script_lines.append(f"[Khung hình {seq_id}]: {text}")
        
    script_content = "\n\n".join(script_lines)
    
    # Write to target path
    target_dir = Path("data/ong_vo_gia_va_chau_chi_ton/image/recut") / f"chap_{chapter_num}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "script.txt"
    
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(script_content)
        
    print(f"Saved {len(rows)} script panels to {target_file}")
    
    # Run the import script
    cmd = [
        "venv/bin/python", "src/cli/import_agent_script.py",
        "--chapter", chapter_num,
        "--project", project_title,
        "--script", str(target_file)
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    print(f"Running command: {' '.join(cmd)}")
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print("STDOUT:")
    print(res.stdout)
    print("STDERR:")
    print(res.stderr)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to import script for chapter {chapter_num}")

def main():
    csv_path = Path("data/ong_vo_gia_va_chau_chi_ton/memory/script.csv")
    if not csv_path.exists():
        print(f"Error: {csv_path} does not exist.")
        sys.exit(1)
        
    project_title = "Ông Võ Giả Và Cháu Chí Tôn"
    
    restore_chapter_script(csv_path, "1", project_title)
    restore_chapter_script(csv_path, "2", project_title)
    print("Restoration completed successfully.")

if __name__ == "__main__":
    main()
