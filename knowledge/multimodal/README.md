# Multimodal feature cases

Source: KVV vision prompt-token fixtures and K31 guide §§3.1, 3.3, 3.7.
This category tests image transport/content-part order, tool-history images and
accounting, not visual reasoning quality. No OCRBench or MMMU dataset/score.

Use local fixture bytes, dimensions, resize method and detail value pinned by
hash; never silently fetch a mutable external image. Check multimodal processor
binding → message part order → encoder placeholder expansion → usage accounting.
An image in a tool message is a separate capability from user-image support.
Counts cannot be reused across checkpoint, crop strategy or frontend accounting
without revalidation. Large image-count/limit probes require explicit selection.
