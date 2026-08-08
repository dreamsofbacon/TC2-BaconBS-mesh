# BaconBS Mesh Protocol

BaconBS Mesh Protocol is the shared language for moving eventually consistent BBS records and gateway replies across lossy, size-constrained radio links.

## Language

**Fragment**:
A text slice positioned within one logical payload by a Unicode character offset. One assembly has zero or more fragments.
_Avoid_: Byte chunk, packet payload

**Fragment Assembly**:
Reconstruction of one logical text payload from base, metadata, and continuation frames. An assembly is complete only when matching fragments continuously cover every character from offset zero through the declared length.
_Avoid_: Blind concatenation, length-sum completion

**Conflicting Overlap**:
An incoming fragment whose covered characters disagree with an already accepted fragment. The fragment is rejected and the assembly remains available for repair; newer content never overwrites accepted content.
_Avoid_: Last-write-wins overlap

**Gap Repair**:
Targeted retransmission of character ranges missing from an incomplete Fragment Assembly. A full resend is the fallback when trustworthy ranges cannot be derived.
_Avoid_: Byte-range repair

## Flagged Ambiguities

**Offset and range units**:
Some existing comments call continuation offsets and repair ranges “bytes.” The wire values are Unicode character offsets and character ranges; encoded byte length is used only to size individual radio frames.

## Example Dialogue

> **Developer:** The fragments cover characters 0–80 and 100–140, so is the Fragment Assembly complete?
>
> **Domain expert:** No. Gap Repair must request characters 80–100 before completion.
>
> **Developer:** A retransmission overlaps characters 60–90 but disagrees at character 72. Should the newer text win?
>
> **Domain expert:** No. That is a Conflicting Overlap: reject it, retain the accepted fragments, and repair the untrusted range or request a full resend.
