#!/usr/bin/env python3
"""
InfluxDB Bucket Clearing Script
Clears all data from a specified InfluxDB bucket using the Python client.

Usage: python3 clear_bucket.py <bucket_name>
Example: python3 clear_bucket.py fcclab
"""

import sys
import argparse
from datetime import datetime, timezone
from influxdb_client import InfluxDBClient
from influxdb_client.client.delete_api import DeleteApi

# InfluxDB configuration (same as stream_subscriber.py)
INFLUXDB_URL = "http://localhost:8086"
INFLUXDB_ORG = "fcclab"
INFLUXDB_TOKEN = "fcclab_token"

def clear_bucket(bucket_name):
    """
    Clear all data from the specified InfluxDB bucket.

    Args:
        bucket_name (str): Name of the bucket to clear

    Returns:
        bool: True if successful, False if failed
    """
    client = None
    try:
        # Connect to InfluxDB
        print(f"Connecting to InfluxDB at {INFLUXDB_URL}...")
        client = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG
        )

        # Verify connection
        health = client.health()
        if health.status != "pass":
            print(f"❌ InfluxDB connection failed: {health.message}")
            return False

        print(f"✅ Connected to InfluxDB (org: {INFLUXDB_ORG})")

        # Get delete API
        delete_api = client.delete_api()

        # Delete all data from the bucket
        # Use a very wide time range to cover all possible data
        start_time = datetime(1970, 1, 1, tzinfo=timezone.utc)
        stop_time = datetime.now(timezone.utc)

        print(f"🗑️  Clearing bucket '{bucket_name}'...")
        print(f"   Time range: {start_time} to {stop_time}")

        # Delete data
        delete_api.delete(
            start=start_time,
            stop=stop_time,
            bucket=bucket_name,
            org=INFLUXDB_ORG,
            predicate=""  # Empty predicate deletes all data
        )

        print(f"✅ Bucket '{bucket_name}' cleared successfully!")
        return True

    except Exception as e:
        print(f"❌ Failed to clear bucket '{bucket_name}': {str(e)}")
        return False

    finally:
        if client:
            client.close()

def main():
    """Main function to handle command line arguments and user interaction."""
    parser = argparse.ArgumentParser(
        description="Clear all data from an InfluxDB bucket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 clear_bucket.py fcclab
  python3 clear_bucket.py my_bucket

⚠️  WARNING: This will permanently delete ALL data in the specified bucket!
        """
    )

    parser.add_argument(
        "bucket_name",
        help="Name of the InfluxDB bucket to clear"
    )

    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt"
    )

    args = parser.parse_args()

    # Safety check - require explicit confirmation unless --yes is used
    if not args.yes:
        print("==========================================")
        print(f"Clearing InfluxDB bucket: {args.bucket_name}")
        print("==========================================")
        print(f"Organization: {INFLUXDB_ORG}")
        print(f"InfluxDB URL: {INFLUXDB_URL}")
        print()
        print(f"⚠️  WARNING: This will delete ALL data in bucket '{args.bucket_name}'")
        try:
            response = input("Are you sure you want to continue? (y/N): ").strip().lower()
            if response not in ['y', 'yes']:
                print("Operation cancelled.")
                sys.exit(0)
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            sys.exit(0)
        print()

    # Clear the bucket
    success = clear_bucket(args.bucket_name)

    # Exit with appropriate code
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()