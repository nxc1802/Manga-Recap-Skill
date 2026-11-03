import sys
import os
from pathlib import Path

# Add project root to python path first
project_root = str(Path(__file__).resolve().parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.supabase_client import supabase

def delete_project_data(project_id, title):
    print(f"Deleting data for project '{title}' (ID: {project_id})...")
    
    # 1. Fetch chapters
    chap_res = supabase.table("chapters").select("id").eq("project_id", project_id).execute()
    chapter_ids = [c["id"] for c in chap_res.data]
    
    if chapter_ids:
        # 2. Delete character embeddings associated with this project
        supabase.table("character_embeddings").delete().eq("project_id", project_id).execute()
        print(" -> Deleted character embeddings")
        
        # 3. For each chapter, delete script segments and panels
        for cid in chapter_ids:
            supabase.table("script_segments").delete().eq("chapter_id", cid).execute()
            supabase.table("panels").delete().eq("chapter_id", cid).execute()
            print(f" -> Deleted script segments and panels for chapter: {cid}")
            
        # 4. Delete chapters
        supabase.table("chapters").delete().eq("project_id", project_id).execute()
        print(" -> Deleted chapters")
        
    # 5. Delete project
    supabase.table("projects").delete().eq("id", project_id).execute()
    print(f"Successfully deleted project '{title}'\n")

def main():
    print("Fetching all projects from database...")
    res = supabase.table("projects").select("id, title").execute()
    projects = res.data
    
    if not projects:
        print("No projects found in the database. Nothing to wipe.")
        return
        
    print(f"Found {len(projects)} projects. Wiping all data...")
    for proj in projects:
        try:
            delete_project_data(proj["id"], proj["title"])
        except Exception as e:
            print(f"Error deleting project '{proj['title']}': {e}")
            
    print("Database wiping completed.")

if __name__ == "__main__":
    main()
