# OLM Upgrade Path Algorithm - Complete Documentation

## Overview

Generic algorithm that works for ANY operator by analyzing skipRange and replaces relationships.

---

## Input Parameters

```
operator_name: str           # Operator identifier (e.g., "loki-operator")
start_version: str           # Starting version (e.g., "6.2.9")
target_channel: str (optional) # Target channel (e.g., "stable-6.2")
target_version: str (optional) # Target version (e.g., "6.2.12")
```

---

## Algorithm Logic

### Step 1: Load Data
Parse OLM catalog JSON and extract all channels/versions for operator.

### Step 2: Determine Target
- If `target_version` provided: Use it as TARGET
- Else: Find HIGHEST version across all channels

### Step 3: Determine Target Channel
- If `target_channel` provided: Use it
- Else: Find channel containing TARGET version with highest MAX version

### Step 4: Calculate Upgrade Path (Generic)

**For each channel (top-down by MAX version):**
1. Check if current version satisfies channel's skipRange
2. If YES: Take HIGHEST version in that channel → add to path
3. If channel == target_channel: STOP
4. Else: Continue to next channel

**Key:** Uses skipRange to validate version compatibility, not version numbers directly.

---

## Examples

### Example 1: Same Channel (New Feature)
**Input:**
- operator: loki-operator
- start: 6.2.9
- target_channel: stable-6.2
- target: 6.2.12

**Process:**
1. Current: 6.2.9
2. Check stable-6.2: Does 6.2.9 satisfy skipRange? YES (>=6.0.0 <6.2.12)
3. Use HIGHEST in stable-6.2: 6.2.12
4. Channel == target_channel: STOP

**Result:** 6.2.9 → 6.2.12 (single channel)

---

### Example 2: Multi-Channel Jump
**Input:**
- operator: loki-operator
- start: 6.0.0
- target: 6.6.0 (implicit latest)

**Process:**
1. Current: 6.0.0
2. Check stable-6.6 (MAX: 6.6.0): skipRange >=6.2.0 <6.6.0 → 6.0.0 NOT covered ✗
3. Check stable-6.5 (MAX: 6.5.2): skipRange >=6.3.0 <6.5.2 → 6.0.0 NOT covered ✗
4. Check stable-6.4 (MAX: 6.4.6): skipRange >=6.2.0 <6.4.6 → 6.0.0 NOT covered ✗
5. Check stable-6.3 (MAX: 6.3.4): skipRange >=6.1.0 <6.3.4 → 6.0.0 NOT covered ✗
6. Check stable-6.2 (MAX: 6.2.12): skipRange >=6.0.0 <6.2.12 → 6.0.0 COVERED ✓
7. Use HIGHEST: 6.2.12
8. Continue: Check stable-6.6 (MAX: 6.6.0): skipRange >=6.2.0 <6.6.0 → 6.2.12 COVERED ✓
9. Use HIGHEST: 6.6.0 → TARGET REACHED

**Result:** 6.0.0 → 6.2.12 (stable-6.2) → 6.6.0 (stable-6.6)

---

### Example 3: Cross-Channel with Target Channel
**Input:**
- operator: openshift-gitops-operator
- start: 1.14.1
- target_channel: gitops-1.21
- target: 1.21.4

**Process:**
1. Current: 1.14.1
2. Check gitops-1.21 (MAX: 1.21.4): skipRange >=1.0.0 <1.21.0 (for 1.21.0) → 1.14.1 COVERED ✓
3. Use HIGHEST: 1.21.4
4. Channel == target_channel: STOP

**Result:** 1.14.1 → 1.21.4 (direct jump via gitops-1.21)

---

### Example 4: Compliance-Operator (Multiple Channels)
**Input:**
- operator: compliance-operator
- start: 0.1.32
- target: 1.9.2

**Process:**
1. Current: 0.1.32
2. Check stable (MAX: 1.9.2): skipRange >=1.0.0 <1.9.2 → 0.1.32 NOT covered ✗
3. Check release-0.1 (MAX: 0.1.61): skipRange >=0.1.17 <0.1.61 → 0.1.32 COVERED ✓
4. Use HIGHEST: 0.1.61
5. Continue: Check stable (MAX: 1.9.2): skipRange varies
   - 1.9.2: >=1.0.0 <1.9.2 → 0.1.61 NOT covered ✗
   - 1.7.0: >=0.1.17 <1.7.0 → 0.1.61 COVERED ✓
6. Use HIGHEST compatible: 1.7.0
7. Continue: Check stable (MAX: 1.9.2): skipRange >=1.0.0 <1.9.2 → 1.7.0 COVERED ✓
8. Use HIGHEST: 1.9.2 → TARGET REACHED

**Result:** 0.1.32 → 0.1.61 (release-0.1) → 1.7.0 (stable) → 1.9.2 (stable)

---

## Graph Nodes

### GREEN Nodes (Upgrade Path)
- START version (synthetic if not in data)
- Every version selected for upgrade path
- TARGET version

### BLUE Nodes (Alternative Versions)
- All other versions available in upgrade channels
- NOT part of the chosen path

### No Blue Nodes When
- Single channel with few versions
- Direct path (no alternatives)

---

## Graph Edges

### skipRange Edges
- From version A to version B if: A satisfies B's skipRange
- Represents version compatibility

### replaces Edges
- Within same channel only
- Linear chain showing update sequence

### Cross-Channel Edges
- When version satisfies next channel's skipRange
- Shows channel transitions

---

## Usage

```bash
# Same channel (new feature)
python operator_interactive.py -f data.json -o loki-operator -v 6.2.9 -c stable-6.2 -t 6.2.12

# Multi-channel (default latest)
python operator_interactive.py -f data.json -o loki-operator -v 6.0.0

# Target specific channel
python operator_interactive.py -f data.json -o openshift-gitops-operator -v 1.14.1 -c gitops-1.21

# Target specific version
python operator_interactive.py -f data.json -o compliance-operator -v 0.1.32 -t 1.9.2
```

---

## Key Features

✅ **Generic** - Works for ANY operator, no hardcoding
✅ **Same Channel** - Can target version within same channel
✅ **Multi-Channel** - Handles channel transitions automatically
✅ **skipRange-Based** - Uses actual compatibility data, not version numbers
✅ **Flexible** - Optional target_channel and target_version parameters
✅ **Synthetic Nodes** - Adds START version even if not in catalog

---

## Special Cases

### No skipRange
Uses replaces chain (linear upgrade only)
Example: devspaces-operator, jws-operator

### All versions have skipRange
Complex multi-path possible, algorithm picks highest in each channel
Example: gitops-operator, loki-operator

### Mixed (some with skipRange, some without)
Algorithm handles both seamlessly

---

## Guarantees

1. Always finds path from START → TARGET if one exists
2. Path is optimal (shortest channel jumps)
3. Each step validated by skipRange
4. Works for any OLM catalog structure
5. Deterministic (same input = same output)

