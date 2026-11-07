# traceDB

Fast, byte-budgeted, content-addressed store for agent traces and research data, over S3 (or a local dir).
Pairs with [ontolagni](https://github.com/Bobliuuu/ontolagni): ontolagni says *where* data is, traceDB fetches it.

See DESIGN.md. Quick use:

    export TRACEDB_STORE=s3://bucket/prefix      # or a local dir
    tracedb put ./logs --prefix logs/
    tracedb locate logs/x/y.jsonl
    tracedb plan logs/x/y.jsonl                  # cost, no fetch
    tracedb get logs/x/y.jsonl --head 4096       # budgeted (default 8 MiB cap)
