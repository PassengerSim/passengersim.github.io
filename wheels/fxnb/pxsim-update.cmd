#!/usr/bin/env zsh

cd ${HOME}/pxsim
cd passengersim-public
git pull
cd ../passengersim-core
git pull
cd ../passengersim-dev
git pull
cd ../passengersim-verification
git pull
cd ..

eval "$(conda shell.bash hook)"
conda activate .env/HANGAR

# compile and install (public python-only code in editable mode)
python -m pip install -v ./passengersim-core
python -m pip install -v -e ./passengersim-public

# run unit tests
python -m pytest -v ./passengersim-core/tests
python -m pytest -v ./passengersim-public/tests
