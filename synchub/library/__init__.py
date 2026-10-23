"""The Library: analysis records, plain-language recommendations and approvals for the files on the shares.

This is the original "Media Analysis and Migration" tool's decision logic, kept nearly unchanged, over SyncHub's database:
the workers look at files (ffprobe, naming, tracks, integrity) and report; everything that decides runs here.
"""
