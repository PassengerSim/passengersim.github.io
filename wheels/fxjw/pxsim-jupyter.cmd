#!/usr/bin/env zsh
eval "$(conda shell.bash hook)"
cd -- "$(dirname "$0")"
cd ..
conda activate .env/HANGAR
jupyter lab
