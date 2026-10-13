set -e
apt-get update -qq >/dev/null && apt-get install -y -qq gcc make git gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi >/dev/null 2>&1
cd /work && rm -rf k && git clone -q https://github.com/Klipper3d/klipper k && cd k && git checkout -q ce7002bedf37e938bb483572949f3703ac6476cb
cp /work/xlplus_rs485.c /work/xlplus_resets.c src/stm32/
echo 'src-$(CONFIG_MACH_STM32F4) += stm32/xlplus_rs485.c stm32/xlplus_resets.c' >> src/stm32/Makefile
printf 'CONFIG_LOW_LEVEL_OPTIONS=y\nCONFIG_MACH_STM32=y\nCONFIG_MACH_STM32F407=y\nCONFIG_STM32_FLASH_START_20200=y\nCONFIG_STM32_CLOCK_REF_12M=y\nCONFIG_STM32_USB_PA11_PA12=y\nCONFIG_USB_VENDOR_ID=0x1d50\nCONFIG_USB_DEVICE_ID=0x614e\n' > .config
echo 'CONFIG_INITIAL_PINS="PE14"' >> .config
make -s olddefconfig >/dev/null; grep -E "^CONFIG_FLASH_APPLICATION_ADDRESS|^CONFIG_INITIAL_PINS" .config
echo "--- compiler output:"; make -s -j8 2>&1 | sed 's/^/  /' || true
echo "--- end"
ls -la out/klipper.bin; arm-none-eabi-readelf -h out/klipper.elf | grep Entry; md5sum out/klipper.bin
python3 -c "import json;d=json.load(open('out/klipper.dict'));print(sorted(k for k in d['commands'] if any(w in k for w in ('bridge','rs485','xl_reset'))));print(sorted(k for k in d['responses'] if any(w in k for w in ('bridge','rs485','xl_reset'))));print({k:v for k,v in d['config'].items() if 'PROTOCOL' in k})"
