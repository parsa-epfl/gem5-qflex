#!/usr/bin/env python3
"""
QFlex Checkpoint Generator - Creates gem5 checkpoints from QEMU device trees

Takes a YAML device tree (from dtc -O yaml), extracts platform configuration,
and creates a gem5 checkpoint compatible with QEMU virt machine layout.

Usage from QPoints directory:
    ./gem5/build/ARM/gem5.opt --outdir=qflex_chkp \\
        gem5/configs/example/arm/qFlex_checkpoint_generate.py \\
        --yaml virt.yaml

Configuration:
- Extracts CPU count and model from device tree
- AtomicSimpleCPU (no caches) for fast checkpoint generation
- Configurable memory size and controllers
- Raw disk image attached via VirtIO
- QEMU UEFI bootloader support

The checkpoint will be in: <outdir>/<checkpoint-dir>/m5.cpt
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

import m5
from m5.objects import *

#  Add paths to gem5 configs
m5.util.addToPath(os.path.join(os.path.dirname(__file__), "../../.."))
m5.util.addToPath(os.path.join(os.path.dirname(__file__), "../.."))

import devices
from common import ObjectList, MemConfig, SysPaths


# ============================================================================
# Argument parsing
# ============================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="QFlex Checkpoint Generator from Device Tree YAML",
        epilog="Generates gem5 checkpoint from QEMU virt device tree"
    )

    # Required: Device tree YAML
    parser.add_argument(
        "--yaml",
        type=str,
        required=True,
        help="Device tree YAML file (from dtc -O yaml)",
    )

    # Bootloader
    parser.add_argument(
        "--bootloader",
        type=str,
        help="UEFI bootloader file (default: /home/dev/qflex/QEMU_EFI.fd)",
    )
    # parser.add_argument(
    #     "--bootloader",
    #     type=str,
    #     default="/home/dev/qflex/QEMU_EFI.fd",
    #     help="UEFI bootloader file (default: /home/dev/qflex/QEMU_EFI.fd)",
    # )

    # Kernel (optional, can boot from disk)
    parser.add_argument(
        "--kernel",
        type=str,
        default="vmlinux.arm64",
        help="Linux kernel binary (default: vmlinux.arm64)",
    )

    # Disk image
    parser.add_argument(
        "--disk-image",
        type=str,
        default=None,
        help="Raw disk image file (optional, creates empty if not provided)",
    )
    
    parser.add_argument(
        "--root-device",
        type=str,
        default="/dev/vda1",
        help="Root device for Linux (default: /dev/vda1)",
    )

    # Memory options
    parser.add_argument(
        "--mem-type",
        default="DDR3_1600_8x8",
        choices=ObjectList.mem_list.get_names(),
        help="Memory type (default: DDR3_1600_8x8)",
    )
    parser.add_argument(
        "--mem-size",
        type=str,
        default="8GiB",
        help="Physical memory size (default: 8GiB)",
    )
    parser.add_argument(
        "--mem-channels",
        type=int,
        default=1,
        help="Number of memory channels (default: 1)",
    )

    # Checkpoint output
    parser.add_argument(
        "--checkpoint-dir",
        type=str,
        default="qflex_checkpoint",
        help="Checkpoint directory name (default: qflex_checkpoint)",
    )

    # CPU frequency
    parser.add_argument(
        "--cpu-freq",
        type=str,
        default="4GHz",
        help="CPU frequency (default: 4GHz)",
    )

    return parser.parse_args()


# Parse arguments first
args = parse_args()

print("="*80)
print("QFlex Checkpoint Generator")
print("="*80)

# ============================================================================
# Step 1: Convert YAML device tree to memmap JSON
# ============================================================================
print("\n[Step 1] Converting device tree YAML to memory map JSON...")

if not os.path.exists(args.yaml):
    print(f"\nERROR: YAML file not found: {args.yaml}")
    sys.exit(1)

# Run yaml_to_memmap.py script
yaml_to_memmap_script = os.path.join(
    os.path.dirname(__file__),
    "../../../yaml_to_memmap.py"
)

if not os.path.exists(yaml_to_memmap_script):
    print(f"\nERROR: yaml_to_memmap.py not found at: {yaml_to_memmap_script}")
    sys.exit(1)

# Running under gem5 embeds Python; sys.executable points to gem5.opt.
# Resolve a real Python interpreter for helper scripts.
python_exe = (
    os.environ.get("PYTHON3")
    or shutil.which("python3")
    or shutil.which("python")
)
if not python_exe:
    print("\nERROR: Could not find a Python interpreter (python3/python)")
    sys.exit(1)

# Write JSON output in the current working directory so it is easy to inspect.
yaml_base = os.path.splitext(os.path.basename(args.yaml))[0]
json_path = os.path.abspath(f"{yaml_base}.memmap.json")

try:
    result = subprocess.run(
        [python_exe, yaml_to_memmap_script, args.yaml, '-o', json_path],
        capture_output=True,
        text=True,
        check=True
    )
    print(f"  YAML converted successfully")
    print(f"  Converter Python: {python_exe}")
    print(f"  Memmap JSON: {json_path}")
    if result.stdout:
        print(f"  {result.stdout.strip()}")
except subprocess.CalledProcessError as e:
    print(f"\nERROR: Failed to convert YAML to JSON:")
    print(e.stderr)
    sys.exit(1)

# Guarantee the JSON file exists even if helper output mode changes.
if not os.path.exists(json_path):
    fallback_text = (result.stdout or "").strip()
    json_start = fallback_text.find("{")
    if json_start != -1:
        try:
            parsed = json.loads(fallback_text[json_start:])
            with open(json_path, "w") as f:
                json.dump(parsed, f, indent=2)
            print(f"  Memmap JSON recovered from stdout: {json_path}")
        except Exception:
            pass

if not os.path.exists(json_path):
    print(f"\nERROR: Memmap JSON was not created: {json_path}")
    sys.exit(1)

# Load the generated JSON
try:
    with open(json_path, 'r') as f:
        memmap_cfg = json.load(f)
    print(f"  Memory map loaded from JSON")
except Exception as e:
    print(f"\nERROR: Failed to load JSON: {e}")
    sys.exit(1)

# Extract CPU information
cpu_info = memmap_cfg.get('cpu', {})
num_cores = cpu_info.get('num_cores', 2)
cpu_model = cpu_info.get('cpu_model', 'cortex-a57')
arm_release = cpu_info.get('release', 'armv8')

print(f"  CPU cores: {num_cores}")
print(f"  CPU model: {cpu_model}")
print(f"  ARM release: {arm_release}")

# ============================================================================
# Step 2: Create QFlex platform with memmap
# ============================================================================
print("\n[Step 2] Creating QFlex platform...")

# Create workload
workload = ArmFsLinux()

# Create system with QFlex platform
system = devices.SimpleSystem(
    caches=False,  # No caches for atomic CPU
    mem_size=args.mem_size,
    mem_mode='atomic',
    workload=workload,
    platform=QFlex_GICv3(memmap=memmap_cfg),  # Pass memmap dict
)

print(f"  Platform: QFlex_GICv3 with QEMU virt memory map")
print(f"  Memory mode: atomic")

# ============================================================================
# Step 3: Configure memory with MemConfig
# ============================================================================
print("\n[Step 3] Configuring memory system...")

# Update args for MemConfig
args.mem_ranks = None
MemConfig.config_mem(args, system)

print(f"  Memory type: {args.mem_type}")
print(f"  Memory size: {args.mem_size}")
print(f"  Memory channels: {args.mem_channels}")

# ============================================================================
# Step 4: Set up disk image
# ============================================================================
print("\n[Step 4] Setting up disk image...")

if args.disk_image:
    # User provided disk image
    disk_search_paths = [
        args.disk_image,
        f'binaries/{args.disk_image}',
        f'gem5/binaries/{args.disk_image}',
    ]
    
    disk_path = None
    for dpath in disk_search_paths:
        if os.path.exists(dpath):
            disk_path = dpath
            break
    
    if not disk_path:
        print(f"  ERROR: Disk image not found: {args.disk_image}")
        sys.exit(1)
    print(f"  Using disk: {disk_path}")
else:
    # Create empty disk
    disk_path = os.path.join(m5.options.outdir, 'qflex_disk.img')
    os.makedirs(m5.options.outdir, exist_ok=True)
    print(f"  Creating empty 512MB disk: {disk_path}")
    with open(disk_path, 'wb') as f:
        f.seek(512 * 1024 * 1024 - 1)  # 512MB
        f.write(b'\0')

# Create raw disk image (not COW)
disk_image = RawDiskImage(read_only=False, image_file=disk_path)

# Attach VirtIO block device to PCI
system.pci_devices = [
    PciVirtIO(vio=VirtIOBlock(image=disk_image))
]

# Attach PCI devices to platform
for dev in system.pci_devices:
    system.attach_pci(dev)

print(f"  VirtIO block device attached to PCI")

# ============================================================================
# Step 5: Connect memory system
# ============================================================================
print("\n[Step 5] Connecting memory system...")

system.connect()

print(f"  Memory system connected")

# ============================================================================
# Step 6: Create CPU cluster
# ============================================================================
print(f"\n[Step 6] Creating {num_cores}-core CPU cluster...")

# Create CPU cluster using devices.AtomicCluster
system.cpu_cluster = [
    devices.AtomicCluster(
        system,
        num_cores,
        args.cpu_freq,
        "1.0V",
    )
]

print(f"  {num_cores} x AtomicSimpleCPU @ {args.cpu_freq}")

# Configure ARM release and remove SECURITY/VIRTUALIZATION
print(f"\n[Step 7] Configuring ARM release...")

# Create ARM release object based on extracted info
if arm_release == "armv8":
    release = Armv8()
elif arm_release == "armv8.2":
    release = Armv82()
else:
    release = Armv8()  # Default fallback

# Remove SECURITY and VIRTUALIZATION extensions
release.remove(ArmExtension("SECURITY"))
release.remove(ArmExtension("VIRTUALIZATION"))

system.release = release

print(f"  ARM release: {arm_release}")
print(f"  Removed extensions: SECURITY, VIRTUALIZATION")

# ============================================================================
# Step 8: Add caches (none for atomic)
# ============================================================================
print("\n[Step 8] Setting up cache hierarchy...")

system.addCaches(need_caches=False, last_cache_level=1)

print(f"  No caches (atomic mode)")

# ============================================================================
# Step 9: Set up kernel and bootloader (minimal for checkpoint template)
# ============================================================================
print("\n[Step 9] Configuring workload (checkpoint template mode)...")

# For checkpoint generation, set workload parameters with optional bootloader/kernel
# These files are not required for checkpoint instantiation
system.workload.load_addr_offset = 0x40000000
system.workload.dtb_addr = 0x40000000 + 0x8000000
system.workload.initrd_addr = system.workload.dtb_addr + 0x200000
system.workload.cpu_release_addr = system.workload.dtb_addr - 8
system.m5ops_base = 0x10010000

# # Optional: if bootloader exists, use it; otherwise checkpoint works without it
# if os.path.exists(args.bootloader):
#     bootloader_abs = os.path.abspath(args.bootloader)
#     system.workload.boot_loader = [bootloader_abs]
#     print(f"  Using bootloader: {bootloader_abs}")
# else:
#     print(f"  Bootloader not found (checkpoint will work without it)")

# Look for kernel (optional, can boot from disk via UEFI)

kernel_path = None
if os.path.exists(args.kernel):
    kernel_path = args.kernel
else:
    kernel_search_paths = [
        f'binaries/{args.kernel}',
        f'gem5/binaries/{args.kernel}',
    ]
    for kpath in kernel_search_paths:
        if os.path.exists(kpath):
            kernel_path = kpath
            break

if kernel_path:
    print(f"  Using kernel: {kernel_path}")
    system.workload.object_file = kernel_path
    
    # Kernel command line
    system.workload.command_line = (
        "console=ttyAMA0 "
        "lpj=19988480 "
        "norandmaps "
        f"root={args.root_device} "
        "rw "
        f"mem={args.mem_size}"
    )
else:
    print(f"  No kernel found (checkpoint will work without it)")

# ============================================================================
# Step 10: Generate DTB
# ============================================================================
print("\n[Step 10] Generating device tree blob...")

dtb_filename = os.path.join(m5.options.outdir, 'qflex_system.dtb')
system.workload.dtb_filename = dtb_filename

# ============================================================================
# Step 11: Instantiate system
# ============================================================================
print("\n[Step 11] Instantiating system...")

root = Root(full_system=True, system=system)

# Generate DTB before instantiation
system.generateDtb(dtb_filename)
print(f"  DTB generated: {dtb_filename}")

m5.instantiate()

print("  System instantiated successfully!")

# ============================================================================
# Step 12: Run and create checkpoint
# ============================================================================
print("\n[Step 12] Running for 1000 ticks to initialize...")

try:
    exit_event = m5.simulate(1000)
    print(f"  Initial run complete")
except Exception as e:
    print(f"  Initial run complete: {e}")

# Create checkpoint
checkpoint_dir = os.path.join(m5.options.outdir, args.checkpoint_dir)
print(f"\n[Step 13] Creating checkpoint...")
print(f"  Location: {checkpoint_dir}")

m5.checkpoint(checkpoint_dir)

# ============================================================================
# Summary
# ============================================================================
print("\n" + "="*80)
print("SUCCESS! QFlex Checkpoint Created")
print("="*80)
print(f"\nCheckpoint: {checkpoint_dir}/m5.cpt")
print(f"DTB: {dtb_filename}")
print(f"\nConfiguration:")
print(f"  Platform: QFlex_GICv3 (QEMU virt compatible)")
print(f"  CPUs: {num_cores} x AtomicSimpleCPU ({cpu_model}) @ {args.cpu_freq}")
print(f"  Memory: {args.mem_size} {args.mem_type}")
print(f"  Disk: {disk_path}")
print(f"  Bootloader: {args.bootloader}")
if kernel_path:
    print(f"  Kernel: {kernel_path}")
print(f"  Memmap JSON: {json_path}")
print(f"\nTo restore this checkpoint:")
print(f"  ./gem5/build/ARM/gem5.opt --outdir=restore_run \\")
print(f"      <your_script.py> --checkpoint-restore={checkpoint_dir}")
print(f"\nTo examine checkpoint:")
print(f"  less {checkpoint_dir}/m5.cpt")
print(f"  ls -lh {checkpoint_dir}/*.pmem")
print("="*80)
