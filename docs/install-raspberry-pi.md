# Raspberry Pi installation

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
mkdir -p ~/Projects
cd ~/Projects
git clone https://github.com/Shahar373/dmr-iq-surveyor.git
cd dmr-iq-surveyor
chmod +x scripts/*.sh
./scripts/bootstrap.sh
./scripts/run_shahar_recordings.sh
```

The configured recordings are expected at:

- `/home/shahar/Documents/SDRconnect_IQ_20260713_150242_163671500HZ.wav`
- `/home/shahar/Documents/SDRconnect_IQ_20260713_150256_163671500HZ.wav`
