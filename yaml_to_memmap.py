#!/usr/bin/env python3
"""
Convert device tree YAML (from dtc -O yaml) to QFlex memory map JSON.

Usage:
    dtc -I dts -O yaml -o virt.yaml virt.dts
    python3 yaml_to_memmap.py virt.yaml -o memmap.json
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
        # cpu_max from redistributor size (128KB per CPU in GICv3)
        if reg[1][1] > 0:
            gic['cpu_max'] = reg[1][1] // 0x20000
        
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
            if space_code == 0x01000000:
                # PIO space
                if 'pio_base' not in pci:
                    pci['pio_base'] = hex(parent_addr)
            elif space_code == 0x02000000 or space_code == 0x03000000:
                # MMIO space (take the first one, typically 32-bit)
                if 'mmio_base' not in pci:
                    pci['mmio_base'] = hex(parent_addr)
    
    # Interrupt mapping
    int_map = get_int_list(node, 'interrupt-map')
    if int_map and len(int_map) >= 10:
        # Extract first interrupt number (typically at index 8)
        # Format: <child_unit_addr child_irq ... parent_phandle parent_irq flags>
        pci['int_base'] = int_map[8]
        pci['int_count'] = 4  # Typical INTA-INTD
    
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

def yaml_to_memmap(yaml_data: List[Dict]) -> Dict:
    """
    Convert device tree YAML to QFlex memory map.
    
    Args:
        yaml_data: Parsed YAML data (list with one root dict)
    
    Returns:
        Memory map dictionary
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
                if reg:
                    memmap['uart'] = {
                        'base': hex(reg[0][0]),
                        'size': hex(reg[0][1]),
                        'irq': irqs[0]['num'] if irqs else 1,
                    }
            
            # ---- RTC (PL031) ----
            elif 'pl031' in node_name or contains_string(node_data, 'compatible', 'pl031'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                irqs = decode_interrupts(node_data)
                if reg:
                    memmap['rtc'] = {
                        'base': hex(reg[0][0]),
                        'size': hex(reg[0][1]),
                        'irq': irqs[0]['num'] if irqs else 2,
                    }
            
            # ---- VirtIO MMIO ----
            elif node_name.startswith('virtio_mmio@') or contains_string(node_data, 'compatible', 'virtio,mmio'):
                reg = decode_reg(node_data, parent_ac, parent_sc)
                irqs = decode_interrupts(node_data)
                if reg:
                    virtio_list.append({
                        'base': hex(reg[0][0]),
                        'size': hex(reg[0][1]),
                        'irq': irqs[0]['num'] if irqs else 16,
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
    
    # Keep only the first VirtIO slot
    if virtio_list:
        memmap['virtio'] = [virtio_list[0]]
    
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
    
    Bridge ranges define which address ranges the system bus forwards to the IO bus.
    """
    # Collect OFF-CHIP IO device addresses only.
    # Do NOT include GIC here: GIC is attached on-chip in RealView and
    # adding it to bridge ranges makes iobridge overlap with gic.pio.
    io_addrs = []
    
    # Add peripheral addresses
    for key in ['uart', 'rtc']:
        if key in memmap and 'base' in memmap[key]:
            addr = int(memmap[key]['base'], 16)
            io_addrs.append(addr)
    
    if 'virtio' in memmap:
        for dev in memmap['virtio']:
            if 'base' in dev:
                addr = int(dev['base'], 16)
                io_addrs.append(addr)
    
    if not io_addrs:
        return []
    
    # Group into ranges
    io_addrs.sort()
    ranges = []
    
    # Range 1: off-chip peripherals (UART, RTC, VirtIO)
    # Round down to nearest 16MB boundary for start
    io_start = (io_addrs[0] // 0x1000000) * 0x1000000
    # Round up to nearest 16MB boundary for end, with some headroom
    io_end = ((io_addrs[-1] + 0x2000000) // 0x1000000) * 0x1000000
    ranges.append([hex(io_start), hex(io_end)])
    
    # Range 2: PCI MMIO space
    if 'pci' in memmap and 'mmio_base' in memmap['pci']:
        mmio_base = int(memmap['pci']['mmio_base'], 16)
        dram_base = int(memmap['dram']['base'], 16) if 'dram' in memmap else 0x40000000
        ranges.append([hex(mmio_base), hex(dram_base)])
    
    return ranges


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Convert device tree YAML to QFlex memory map JSON'
    )
    parser.add_argument('yaml_file', help='Input YAML file (from dtc -O yaml)')
    parser.add_argument('-o', '--output', help='Output JSON file (default: stdout)')
    
    args = parser.parse_args()
    
    # Load YAML
    with open(args.yaml_file, 'r') as f:
        yaml_data = yaml.safe_load(f)
    
    # Convert to memmap
    memmap = yaml_to_memmap(yaml_data)
    
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
