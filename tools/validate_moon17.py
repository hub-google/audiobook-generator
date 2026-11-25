"""Smoke-test the actual HTTP Moon17 adapter, not a blocked browser route."""
import json
import sys
from tools.validate_sources_live import live_adapter


def main():
    report=live_adapter("moon17")
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return 0 if report["status"]=="PASS" else 1


if __name__=="__main__":
    sys.exit(main())
