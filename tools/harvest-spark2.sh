#!/bin/bash
# pull RSI-relevant data from spark2 (ssh alias only) into ~/boxharvest/spark2-<date>/. No sudo, read-only on the box.
D=20261112; OUT=$HOME/boxharvest/spark2-$D; mkdir -p $OUT
ssh -o ConnectTimeout=20 -o BatchMode=yes spark2 'hostname; df -h /home/jerry|tail -1; ls -la /home/jerry; du -sh /home/jerry/* /home/jerry/.[a-z]* 2>/dev/null|sort -h|tail -40; nvidia-smi -L 2>/dev/null; ps -eo user,etime,cmd --sort=-etime | grep -i -E "rsi|inference|python" | grep -v grep | head -20' > $OUT/INVENTORY.txt 2>/dev/null || { rm -f $OUT/INVENTORY.txt; exit 1; }
# everything except caches/venvs/model weights; files > 200M are listed, then streamed separately (see big-files.txt)
ssh spark2 'find /home/jerry -xdev -type f -size +200M -not -path "*/.cache/*" -not -path "*/venv*" -not -path "*/.venv*" -printf "%s %p\n" 2>/dev/null' > $OUT/big-files.txt
rsync -a --partial -e ssh --max-size=200M --prune-empty-dirs \
  --exclude='.cache/' --exclude='venv*/' --exclude='.venv*/' --exclude='node_modules/' --exclude='__pycache__/' --exclude='.git/objects/' \
  --exclude='*.safetensors' --exclude='*.gguf' --exclude='.ssh/' --exclude='.local/lib/' --exclude='miniconda*/' --exclude='anaconda*/' \
  spark2:/home/jerry/ $OUT/home/ > $OUT/rsync.log 2>&1
echo "rsync exit $?" >> $OUT/INVENTORY.txt
touch $OUT/.pull-done
