#!/usr/bin/env python3
"""
Convenience script to load Astram event CSV data into the database.
Auto-detects the Astram CSV file and loads it with a single command.
"""
from pathlib import Path
import sys
import os

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from backend.data.load_data import load_csv, apply_schema, upsert_events, reseed_police_stations, print_sanity, require_database_url
from sqlalchemy import create_engine


def find_astram_csv() -> Path | None:
    """Auto-detect the Astram CSV file in the project root"""
    project_root = Path(__file__).parent
    for csv_file in project_root.glob("*Astram*event*data*.csv"):
        return csv_file
    for csv_file in project_root.glob("*.csv"):
        if "astram" in csv_file.name.lower() and "event" in csv_file.name.lower():
            return csv_file
    return None


def main():
    csv_path = find_astram_csv()
    if not csv_path:
        print("❌ Could not find Astram CSV file.")
        print("   Expected file matching pattern '*Astram*event*data*.csv' in the project root.")
        sys.exit(1)
    
    print(f"✓ Auto-detected Astram CSV: {csv_path}")
    
    # Verify database URL
    try:
        database_url = require_database_url()
    except SystemExit as e:
        print(f"❌ {e}")
        sys.exit(1)
    
    engine = create_engine(database_url, future=True)
    # Use schema.sql from root directory
    schema_path = Path(__file__).parent / "schema.sql"
    
    print(f"✓ Applying schema from {schema_path}")
    try:
        apply_schema(engine, schema_path)
    except Exception as e:
        print(f"❌ Error applying schema: {e}")
        sys.exit(1)
    
    print(f"✓ Loading CSV data...")
    try:
        data, missing_id_count, duplicate_id_count, negative_duration_count = load_csv(csv_path)
    except Exception as e:
        print(f"❌ Error loading CSV: {e}")
        sys.exit(1)
    
    if missing_id_count:
        print(f"⚠ Dropped {missing_id_count} rows with missing id")
    if duplicate_id_count:
        print(f"⚠ Found {duplicate_id_count} duplicate ids; kept the last row for each")
    if negative_duration_count:
        print(f"⚠ Set {negative_duration_count} negative durations to null")
    
    print(f"✓ Loaded {len(data)} event rows from {csv_path.name}")
    
    try:
        upsert_events(engine, data)
        print(f"✓ Upserted {len(data)} events into database")
    except Exception as e:
        print(f"❌ Error upserting events: {e}")
        sys.exit(1)
    
    try:
        station_count = reseed_police_stations(engine, data)
        print(f"✓ Seeded {station_count} police stations")
    except Exception as e:
        print(f"❌ Error reseeding police stations: {e}")
        sys.exit(1)
    
    print(f"\n✓ Data loading complete! Running sanity check...")
    try:
        print_sanity(engine)
    except Exception as e:
        print(f"⚠ Error running sanity check: {e}")
    
    print("\n✅ Astram event data successfully loaded into the database!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted")
