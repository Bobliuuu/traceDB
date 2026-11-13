#!/usr/bin/env python3
"""strip the full-document wrapper so the Artifact tool's skeleton can wrap the page: prep-artifact.py IN OUT"""
import re, sys
t = open(sys.argv[1]).read()
t = re.sub(r"^<!doctype html><html[^>]*>\s*<meta charset=utf-8>\s*<meta name=viewport[^>]*>\s*", "", t, flags=re.I)
t = re.sub(r"</html>\s*$", "", t)
open(sys.argv[2], "w").write(t)
