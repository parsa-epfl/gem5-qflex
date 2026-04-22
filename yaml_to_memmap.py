#!/usr/bin/env python3
"""
Convert device tree YAML (from dtc -O yaml) to QFlex memory map JSON.

Usage:
    dtc -I dts -O yaml -o virt.yaml virt.dts
    python3 yaml_to_memmap.py virt.yaml -o memmap.json

Options:
    --virtio-devices INDICES   Comma-separated 1-based indices of virtio_mmio
                               nodes to include, in the given order.
                               Example: --virtio-devices 1   (first device only)
                                        --virtio-devices 2,4,1,3
                               If omitted, no virtio devices are included in
                               the output memmap.
"""

import yaml
import json
import argparse
import sys
from typing import Dict, List, Any, Optional, Tuple


# ---------------------------------------------------------------------------
# Helper functions for YAML property extraction
# ---------------------------------------------------------------------------

def get_property(node: Dict, prop_name: str, default=None):
    """Get a property from a YAML node."""
    return node.get(prop_name, default)


def get_int_list(node: Dict, prop_name: str) -> List[int]:
    """Get a property as a flat list of integers."""
    prop = node.get(prop_name)
    if not prop:
        return []
    
    # Properties in YAML are typically [[val1, val2, ...]]
    if isinstance(prop, list) and len(prop) > 0:
        if isinstance(prop[0], list):
            # Flatten the nested list
            result = []
            for sublist in prop:
                result.extend(sublist)
            return result
        return prop
    return []


def get_string_list(node: Dict, prop_name: str) -> List[str]:
    """Get a property as a list of strings."""
    prop = node.get(prop_name)
    if not prop:
        return []
    
    if isinstance(prop, list):
        # Strings may contain null-separated values
        result = []
        for s in prop:
            if isinstance(s, str):
                # Split by null character
                result.extend(s.split('\0'))
        return [s for s in result if s]  # filter empty strings
    return []


def contains_string(node: Dict, prop_name: str, search: str) -> bool:
    """Check if a property contains a specific string."""
    strings = get_string_list(node, prop_name)
    return any(search in s for s in strings)


def decode_reg(node: Dict, addr_cells: int = 2, size_cells: int = 2) -> List[Tuple[int, int]]:
    """
    Decode a 'reg' property into (address, size) tuples.
    
    The reg property is a sequence of (address, size) pairs where
    address takes addr_cells cells and size takes size_cells cells.
    """
    reg_data = get_int_list(node, 'reg')
    if not reg_data:
        return []
    
    result = []
    entry_size = addr_cells + size_cells
    
    for i in range(0, len(reg_data), entry_size):
        if i + entry_size > len(reg_data):
            break
        
        # Decode multi-cell address
        addr = 0
        for j in range(addr_cells):
            addr = (addr << 32) | reg_data[i + j]
        
        # Decode multi-cell size
        size = 0
        for j in range(size_cells):
            size = (size << 32) | reg_data[i + addr_cells + j]
        
        result.append((addr, size))
    
    return result


def decode_interrupts(node: Dict) -> List[Dict[str, int]]:
    """Decode the 'interrupts' property."""
    irq_data = get_int_list(node, 'interrupts')
    if not irq_data:
        return []
    
    # Typically interrupts are in groups of 3: <type irq flags>
    # For GIC: type (0=SPI, 1=PPI), irq number, flags
    result = []
    for i in range(0, len(irq_data), 3):
        if i + 2 < len(irq_data):
            result.append({
                'type': irq_data[i],
                'num': irq_data[i + 1],
                'flags': irq_data[i + 2],
            })
    
    return result


def irq_kind_from_gic_type(int_type: int) -> str:
    """Map GIC interrupt type cell (0/1) to symbolic kind."""
    if int(int_type) == 0:
        return 'SPI'
    if int(int_type) == 1:
        return 'PPI'
    return f'GIC_TYPE_{int(int_type)}'


def arm_int_type_from_flags(flags: int) -> str:
    """Map DT interrupt flags to gem5 ArmInterruptType enum string."""
    mapping = {
        0x1: 'IRQ_TYPE_EDGE_RISING',
        0x2: 'IRQ_TYPE_EDGE_FALLING',
        0x4: 'IRQ_TYPE_LEVEL_HIGH',
        0x8: 'IRQ_TYPE_LEVEL_LOW',
    }
    return mapping.get(int(flags), 'IRQ_TYPE_LEVEL_HIGH')


def format_size(nbytes: int) -> str:
    """Format a size in bytes to a human-readable string."""
    if nbytes >= 1024**3 and nbytes % (1024**3) == 0:
        return f"{nbytes // (1024**3)}GiB"
    if nbytes >= 1024**2 and nbytes % (1024**2) == 0:
        return f"{nbytes // (1024**2)}MiB"
    if nbytes >= 1024 and nbytes % 1024 == 0:
        return f"{nbytes // 1024}KiB"
    return hex(nbytes)


# ---------------------------------------------------------------------------
# Device extraction
# ---------------------------------------------------------------------------

def extract_gic(node_name: str, node: Dict, addr_cells: int, size_cells: int) -> Dict:
    """Extract GIC configuration from intc@ node."""
    gic = {}
    
    # Determine GIC version from compatible string
    if contains_string(node, 'compatible', 'gic-v3'):
        gic['version'] = 3
    elif contains_string(node, 'compatible', 'gic-400') or contains_string(node, 'compatible', 'gic-v2'):
        gic['version'] = 2
    elif contains_string(node, 'compatible', 'cortex-a15-gic'):
        gic['version'] = 2
    else:
        gic['version'] = 3
    
    reg = decode_reg(node, addr_cells, size_cells)
    
    if gic['version'] == 3 and len(reg) >= 2:
        gic['dist_addr'] = hex(reg[0][0])
        gic['redist_addr'] = hex(reg[1][0])

        # Determine whether the DT uses stride-based or regions-based
        # redistributor discovery, and record it as a simple boolean.
        #
        #   is_stride = True  → gem5 style: 'redistributor-stride' present.
        #   is_stride = False → QEMU style: '#redistributor-regions' present.
        #
        # Both describe the same hardware; the flag controls which property
        # gem5 re-emits in its own generated DTB so the OS sees the same
        # layout it booted with.
        stride_cells = get_int_list(node, 'redistributor-stride')
        regions_list = get_int_list(node, '#redistributor-regions')

        # Decode the stride value for cpu_max computation (not stored).
        stride_val = 0x20000  # standard GICv3 per-CPU size (128 KB)
        if stride_cells:
            decoded = 0
            for c in stride_cells:
                decoded = (decoded << 32) | c
            if decoded > 0:
                stride_val = decoded
            gic['is_stride'] = True
        elif regions_list:
            gic['is_stride'] = False
        # If neither property is present, leave is_stride absent so
        # RealView falls back to its default (stride, gem5 native style).

        # cpu_max: total redistributor region size / per-CPU stride.
        if reg[1][1] > 0:
            gic['cpu_max'] = reg[1][1] // stride_val
        
        # Look for ITS child node
        for child_name, child_node in node.items():
            if isinstance(child_node, dict) and ('its' in child_name or contains_string(child_node, 'compatible', 'gic-v3-its')):
                # Get child's address/size cells, default to parent's
                child_ac_list = get_int_list(node, '#address-cells')
                child_sc_list = get_int_list(node, '#size-cells')
                child_ac = child_ac_list[0] if child_ac_list else addr_cells
                child_sc = child_sc_list[0] if child_sc_list else size_cells
                
                its_reg = decode_reg(child_node, child_ac, child_sc)
                if its_reg:
                    gic['its_addr'] = hex(its_reg[0][0])
                break
    
    elif gic['version'] == 2 and len(reg) >= 2:
        gic['dist_addr'] = hex(reg[0][0])
        gic['cpu_addr'] = hex(reg[1][0])
    
    return gic


def extract_pci(node_name: str, node: Dict, addr_cells: int, size_cells: int) -> Dict:
    """Extract PCI host bridge configuration."""
    pci = {}

    # Derive conf_device_bits from the compatible string.
    # The PCI specification defines two config space layouts:
    #   - ECAM (Enhanced Config Access Mechanism): 12 bits per device = 4 KB
    #     per function. Indicated by 'pci-host-ecam-generic' compatible.
    #   - CAM (Config Access Mechanism): 8 bits per device = 256 B per
    #     function. Indicated by 'pci-host-cam-generic' compatible.
    # gem5's GenericPciHost uses conf_device_bits to compute the per-device
    # config space slot size (1 << conf_device_bits) and to locate each
    # function's config page (confBase + bus_addr << conf_device_bits).
    if contains_string(node, 'compatible', 'pci-host-ecam-generic'):
        pci['conf_device_bits'] = 12
    elif contains_string(node, 'compatible', 'pci-host-cam-generic'):
        pci['conf_device_bits'] = 8
    else:
        # Default to ECAM (most common on modern ARM platforms).
        pci['conf_device_bits'] = 12

    # ECAM config space from 'reg'
    reg = decode_reg(node, addr_cells, size_cells)
    if reg:
        pci['ecam_base'] = hex(reg[0][0])
        pci['ecam_size'] = format_size(reg[0][1])
    
    # Parse ranges for PIO and MMIO apertures
    ranges_data = get_int_list(node, 'ranges')
    if ranges_data:
        # PCI ranges: <child_addr_hi child_addr_mid child_addr_lo parent_addr size>
        # child_addr takes 3 cells (flags, addr_hi, addr_lo in 64-bit)
        # parent_addr takes addr_cells (usually 2)
        # size takes size_cells (usually 2)
        
        entry_size = 3 + addr_cells + size_cells  # typically 3 + 2 + 2 = 7
        
        for i in range(0, len(ranges_data), entry_size):
            if i + entry_size > len(ranges_data):
                break
            
            flags = ranges_data[i]
            # child_addr = (ranges_data[i+1] << 32) | ranges_data[i+2]
            
            # parent_addr (addr_cells = 2)
            parent_addr = 0
            for j in range(addr_cells):
                parent_addr = (parent_addr << 32) | ranges_data[i + 3 + j]
            
            # PCI ranges flags (bits 24-31):
            # 0x01000000 = PIO space
            # 0x02000000 = MMIO 32-bit space
            # 0x03000000 = MMIO 64-bit prefetchable space
            space_code = flags & 0x03000000

            # size (size_cells cells, after parent_addr)
            range_size = 0
            for j in range(size_cells):
                range_size = (range_size << 32) | ranges_data[i + 3 + addr_cells + j]

            if space_code == 0x01000000:
                # PIO space
                if 'pio_base' not in pci:
                    pci['pio_base'] = hex(parent_addr)
                    pci['pio_size'] = hex(range_size)
            elif space_code == 0x02000000:
                # MMIO 32-bit (non-prefetchable)
                if 'mmio_base' not in pci:
                    pci['mmio_base'] = hex(parent_addr)
                    pci['mmio_size'] = hex(range_size)
            elif space_code == 0x03000000:
                # MMIO 64-bit prefetchable
                if 'mmio64_base' not in pci:
                    pci['mmio64_base'] = hex(parent_addr)
                    pci['mmio64_size'] = hex(range_size)
    
    # Interrupt mapping
    int_map = get_int_list(node, 'interrupt-map')
    if int_map and len(int_map) >= 10:
        # Each interrupt-map entry is 10 cells wide:
        #   [0..2]  child unit address  (3 cells: flags, addr_hi, addr_lo)
        #   [3]     child interrupt specifier (1 cell: pin number 1-4)
        #   [4]     parent phandle             (1 cell)
        #   [5..6]  parent unit address        (2 cells, zeroed for GIC)
        #   [7]     GIC interrupt type         (0=SPI, 1=PPI)
        #   [8]     GIC interrupt number       (DT-space, SPI starts at 0)
        #   [9]     GIC interrupt flags        (trigger type)
        entry_width = 10
        pci['int_irq_type'] = irq_kind_from_gic_type(int_map[7])
        pci['int_base'] = int_map[8]
        pci['int_type'] = arm_int_type_from_flags(int_map[9])

        # int_count = number of distinct parent IRQ numbers across all entries.
        unique_irqs = set()
        for i in range(0, len(int_map) - entry_width + 1, entry_width):
            unique_irqs.add(int_map[i + 8])
        pci['int_count'] = len(unique_irqs) if unique_irqs else 4

        # Derive gem5 interrupt routing policy from interrupt-map-mask.
        #
        # The interrupt-map-mask is 4 cells: [child_addr_hi, child_addr_mid,
        # child_addr_lo, child_irq_specifier].
        #
        #   ARM_PCI_INT_DEV     — mask has device bits only in addr cells,
        #                         pin cell = 0x0.  One IRQ per device slot.
        #                         Example mask: [0x1800, 0x0, 0x0, 0x0]
        #
        #   ARM_PCI_INT_SWIZZLE — mask has BOTH device bits AND pin bits.
        #                         IRQ rotates by slot so adjacent devices
        #                         never share a line (QEMU virt style).
        #                         Example mask: [0x1800, 0x0, 0x0, 0x7]
        #
        #   ARM_PCI_INT_PIN     — mask has only pin bits in the irq cell,
        #                         no device bits.  Pure pin-based routing.
        #
        int_mask = get_int_list(node, 'interrupt-map-mask')
        if int_mask and len(int_mask) >= 4:
            has_dev_bits = (int_mask[0] & 0x1800) != 0
            has_pin_bits = int_mask[3] != 0
            if has_dev_bits and has_pin_bits:
                pci['int_policy'] = 'ARM_PCI_INT_SWIZZLE'
            elif has_dev_bits:
                pci['int_policy'] = 'ARM_PCI_INT_DEV'
            else:
                pci['int_policy'] = 'ARM_PCI_INT_PIN'
        else:
            # No mask present — default to device-based routing.
            pci['int_policy'] = 'ARM_PCI_INT_DEV'
    else:
        # No interrupt-map: static routing via the device's 'interrupts'
        # property. gem5 will use GenericPciHost::mapPciInterrupt which
        # falls back to the device's interruptLine() value.
        pci['int_policy'] = 'ARM_PCI_INT_STATIC'

    return pci


def extract_cpu_info(root: Dict) -> Dict:
    """
    Extract CPU information from the cpus node.
    
    Returns dict with:
        - num_cores: Number of CPU cores
        - cpu_model: CPU model string (e.g., "cortex-a57")
        - release: ARM architecture release (e.g., "armv8")
    """
    cpus_node = root.get('cpus', {})
    if not cpus_node or not isinstance(cpus_node, dict):
        return {}
    
    cpu_count = 0
    cpu_model = None
    
    # Count CPU nodes and extract model from first CPU
    for node_name, node_data in cpus_node.items():
        if node_name.startswith('cpu@') and isinstance(node_data, dict):
            cpu_count += 1
            
            # Get compatible string from first CPU
            if cpu_model is None:
                compatible = get_string_list(node_data, 'compatible')
                for compat in compatible:
                    # Extract model from "arm,cortex-a57" -> "cortex-a57"
                    if 'arm,' in compat or 'cortex' in compat.lower():
                        cpu_model = compat.replace('arm,', '').strip()
                        break
    
    if cpu_count == 0:
        return {}
    
    # Determine ARM release from CPU model
    release = "armv8"  # Default to ARMv8
    if cpu_model:
        model_lower = cpu_model.lower()
        if 'cortex-a53' in model_lower or 'cortex-a57' in model_lower or 'cortex-a72' in model_lower:
            release = "armv8"
        elif 'cortex-a75' in model_lower or 'cortex-a76' in model_lower:
            release = "armv8.2"
        # Add more mappings as needed
    
    return {
        'num_cores': cpu_count,
        'cpu_model': cpu_model if cpu_model else "cortex-a57",
        'release': release,
    }


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def yaml_to_memmap(yaml_data: List[Dict], virtio_indices: Optional[List[int]] = None) -> Dict:
    """
    Convert device tree YAML to QFlex memory map.

    Args:
        yaml_data:       Parsed YAML data (list with one root dict, as returned
                         by yaml.safe_load on dtc -O yaml output).
        virtio_indices:  Optional list of 1-based indices selecting which
                         virtio_mmio nodes to include, in the given order.
                         Example: [1] selects the first node only.
                                  [2, 4, 1, 3] selects four nodes in that order.
                         If None or empty, no virtio section is written to the
                         output memmap (memmap['virtio'] will be absent).

    Returns:
        Memory map dictionary suitable for passing to QFlex_Virt_Base(memmap=).
    """
    if not yaml_data or not isinstance(yaml_data, list):
        raise ValueError("Invalid YAML format: expected list of nodes")
    
    root = yaml_data[0]  # Root node is the first element
    
    # Get root cell sizes
    addr_cells_list = get_int_list(root, '#address-cells')
    size_cells_list = get_int_list(root, '#size-cells')
    addr_cells = addr_cells_list[0] if addr_cells_list else 2
    size_cells = size_cells_list[0] if size_cells_list else 2
    
    memmap = {
        "name": "",
        "description": "Memory map extracted from device tree YAML",
    }
    
    # Get model/compatible
    model = get_string_list(root, 'model')
    if not model:
        model = get_string_list(root, 'compatible')
    memmap['name'] = model[0] if model else ""
    
    virtio_list = []
    
    def walk_nodes(node_dict: Dict, parent_ac: int = 2, parent_sc: int = 2):
        """Recursively walk through all nodes."""
        for node_name, node_data in node_dict.items():
            if not isinstance(node_data, dict):
                continue  # Skip non-node properties
            
            # ---- Memory (DRAM) ----
            device_type = get_string_list(node_data, 'device_type')
            if 'memory' in device_type or node_name.startswith('memory@'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                if reg:
                    memmap['dram'] = {
                        'base': hex(reg[0][0]),
                        'max_size': '255GiB',
                    }
            
            # ---- Flash (boot memory) ----
            elif node_name.startswith('flash@') or contains_string(node_data, 'compatible', 'cfi-flash'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                if reg:
                    total = sum(r[1] for r in reg)
                    memmap['bootmem'] = {
                        'base': hex(reg[0][0]),
                        'size': format_size(total),
                    }
            
            # ---- UART (PL011) ----
            elif 'pl011' in node_name or contains_string(node_data, 'compatible', 'pl011'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                irqs = decode_interrupts(node_data)
                if reg and irqs:
                    memmap['uart'] = {
                        'base': hex(reg[0][0]),
                        'size': hex(reg[0][1]),
                        'irq': irqs[0]['num'],
                        'irq_type': irq_kind_from_gic_type(irqs[0]['type']),
                        'int_type': arm_int_type_from_flags(irqs[0]['flags']),
                    }
            
            # ---- RTC (PL031) ----
            elif 'pl031' in node_name or contains_string(node_data, 'compatible', 'pl031'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                irqs = decode_interrupts(node_data)
                if reg and irqs:
                    memmap['rtc'] = {
                        'base': hex(reg[0][0]),
                        'size': hex(reg[0][1]),
                        'irq': irqs[0]['num'],
                        'irq_type': irq_kind_from_gic_type(irqs[0]['type']),
                        'int_type': arm_int_type_from_flags(irqs[0]['flags']),
                    }
            
            # ---- VirtIO MMIO ----
            elif node_name.startswith('virtio_mmio@') or contains_string(node_data, 'compatible', 'virtio,mmio'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                irqs = decode_interrupts(node_data)
                if reg and irqs:
                    virtio_list.append({
                        'base': hex(reg[0][0]),
                        'size': hex(reg[0][1]),
                        'irq': irqs[0]['num'],
                        'irq_type': irq_kind_from_gic_type(irqs[0]['type']),
                        'int_type': arm_int_type_from_flags(irqs[0]['flags']),
                    })
            
            # ---- GIC ----
            # Skip ITS nodes - they are handled as children of the GIC
            elif 'its@' not in node_name and (node_name.startswith('intc@') or contains_string(node_data, 'compatible', 'gic-v3') or contains_string(node_data, 'compatible', 'gic-v2')):
                gic = extract_gic(node_name, node_data, parent_ac, parent_sc)
                if gic:
                    memmap['gic'] = gic
            
            # ---- PCI ----
            elif (node_name.startswith('pcie@') or node_name.startswith('pci@')
                  or contains_string(node_data, 'compatible', 'pci-host') or 'pci' in device_type):
                pci = extract_pci(node_name, node_data, parent_ac, parent_sc)
                if pci:
                    memmap['pci'] = pci
            
            # Recurse into children with updated cell sizes
            child_ac_list = get_int_list(node_data, '#address-cells')
            child_sc_list = get_int_list(node_data, '#size-cells')
            child_ac = child_ac_list[0] if child_ac_list else parent_ac
            child_sc = child_sc_list[0] if child_sc_list else parent_sc
            walk_nodes(node_data, child_ac, child_sc)
    
    # Walk all nodes starting from root
    walk_nodes(root, addr_cells, size_cells)
    
    # Select VirtIO devices.
    # virtio_indices is a list of 1-based indices supplied via --virtio-devices
    # (e.g. [2, 4, 1, 3]).  Order is preserved so the caller controls which
    # device appears first in the memmap (and therefore gets gem5 IRQ 0).
    if virtio_list and virtio_indices:
        selected = []
        for idx in virtio_indices:
            if 1 <= idx <= len(virtio_list):
                selected.append(virtio_list[idx - 1])
            else:
                raise ValueError(
                    f"--virtio-devices index {idx} is out of range "
                    f"(found {len(virtio_list)} virtio_mmio nodes)"
                )
        memmap['virtio'] = selected

    # Extract CPU information
    cpu_info = extract_cpu_info(root)
    if cpu_info:
        memmap['cpu'] = cpu_info
    
    # Compute bridge ranges
    memmap['bridge_ranges'] = _compute_bridge_ranges(memmap)
    
    # Add boot configuration
    memmap['boot'] = {
        'dtb_offset': '0x8000000',
        'load_offset': memmap['dram']['base'] if 'dram' in memmap else '0x40000000',
    }
    
    return memmap


def _compute_bridge_ranges(memmap: Dict) -> List[List[str]]:
    """
    Compute bridge_ranges from device addresses.

    bridge_ranges is the set of address ranges that the iobridge forwards
    from the system membus to the off-chip iobus. It must include every
    address that a CPU might access for off-chip devices. We use exact
    base+size values from the DT for each window — no rounding, no
    approximation — so the set is as tight as possible.

    Do NOT include on-chip devices here (e.g. GIC): those are attached
    directly to membus and adding them to bridge_ranges causes an overlap
    that gem5's bus decoder treats as a fatal decode error.
    """
    ranges = []

    # ---- Off-chip peripherals (UART, RTC, VirtIO) ----
    # Each device gets its own exact [base, base+size) range derived
    # directly from the DT 'reg' property.
    for key in ['uart', 'rtc']:
        if key in memmap and 'base' in memmap[key] and 'size' in memmap[key]:
            base = int(memmap[key]['base'], 16)
            size = int(memmap[key]['size'], 16)
            ranges.append([hex(base), hex(base + size)])

    if 'virtio' in memmap:
        for dev in memmap['virtio']:
            if 'base' in dev and 'size' in dev:
                base = int(dev['base'], 16)
                size = int(dev['size'], 16)
                ranges.append([hex(base), hex(base + size)])

    pci = memmap.get('pci', {})

    # ---- PCI PIO window ----
    # Legacy I/O port space. Linux uses this for legacy PCI device I/O BARs.
    if 'pio_base' in pci and 'pio_size' in pci:
        pio_base = int(pci['pio_base'], 16)
        pio_size = int(pci['pio_size'], 16)
        ranges.append([hex(pio_base), hex(pio_base + pio_size)])

    # ---- PCI MMIO32 (non-prefetchable) window ----
    # Linux places 32-bit device BARs here. The iobridge must forward all
    # accesses in this window so they reach the PCI bus.
    if 'mmio_base' in pci and 'mmio_size' in pci:
        mmio_base = int(pci['mmio_base'], 16)
        mmio_size = int(pci['mmio_size'], 16)
        ranges.append([hex(mmio_base), hex(mmio_base + mmio_size)])

    # ---- PCI ECAM config space ----
    # The ECAM window (conf_base / ecam_base) may sit above DRAM (e.g.
    # 0x4010000000 on QEMU virt). Without this entry any config-space
    # access during Linux PCI enumeration would be dropped by the iobridge.
    if 'ecam_base' in pci and 'ecam_size' in pci:
        ecam_base = int(pci['ecam_base'], 16)
        # ecam_size is a string like "256MiB" — parse it through the same
        # helper that gem5 params use by converting to bytes.
        ecam_size_str = pci['ecam_size']
        ecam_size = _parse_size(ecam_size_str)
        ranges.append([hex(ecam_base), hex(ecam_base + ecam_size)])

    # ---- PCI MMIO64 (64-bit prefetchable) window ----
    # This window is above DRAM (e.g. 0x8000000000 on QEMU virt). gem5's
    # PciUpDownBridge discovers used sub-ranges dynamically via BAR
    # programming, but the iobridge is a static filter. Without this entry
    # any CPU access to a 64-bit BAR address is silently dropped.
    if 'mmio64_base' in pci and 'mmio64_size' in pci:
        mmio64_base = int(pci['mmio64_base'], 16)
        mmio64_size = int(pci['mmio64_size'], 16)
        ranges.append([hex(mmio64_base), hex(mmio64_base + mmio64_size)])

    return ranges


def _parse_size(size_str: str) -> int:
    """Parse a human-readable size string (e.g. '256MiB', '4KiB') to bytes."""
    size_str = size_str.strip()
    units = {'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3,
             'KB': 1000, 'MB': 1000**2, 'GB': 1000**3}
    for suffix, mult in units.items():
        if size_str.endswith(suffix):
            return int(size_str[:-len(suffix)]) * mult
    # Try plain integer or hex
    return int(size_str, 0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Convert device tree YAML to QFlex memory map JSON'
    )
    parser.add_argument('yaml_file', help='Input YAML file (from dtc -O yaml)')
    parser.add_argument('-o', '--output', help='Output JSON file (default: stdout)')
    parser.add_argument(
        '--virtio-devices',
        metavar='INDICES',
        help=(
            'Comma-separated 1-based indices of virtio_mmio nodes to include '
            'in the output memmap, in the given order.  '
            'Example: --virtio-devices 1  (first device only)  '
            '         --virtio-devices 2,4,1,3  '
            'If omitted, no virtio devices are included in the output memmap.'
        ),
    )

    args = parser.parse_args()

    virtio_indices = None
    if args.virtio_devices:
        try:
            virtio_indices = [int(x.strip()) for x in args.virtio_devices.split(',')]
        except ValueError:
            parser.error('--virtio-devices must be a comma-separated list of integers')

    # Load YAML
    with open(args.yaml_file, 'r') as f:
        yaml_data = yaml.safe_load(f)

    # Convert to memmap
    memmap = yaml_to_memmap(yaml_data, virtio_indices=virtio_indices)
    
    # Output JSON
    json_str = json.dumps(memmap, indent=2)
    
    if args.output:
        with open(args.output, 'w') as f:
            f.write(json_str)
        print(f"Memory map written to {args.output}")
    else:
        print(json_str)


if __name__ == '__main__':
    main()
