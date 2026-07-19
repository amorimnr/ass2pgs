# Architecture

## Boundaries

The CLI in `cli.py` only parses arguments and calls the internal conversion
API. Single-file orchestration lives in `pipeline.py`; recursive discovery,
parallel files, resume state, and failure isolation live in `library.py`.

External tools have deliberately narrow roles:

- `mkvmerge -J` identifies tracks and media properties.
- `mkvextract` extracts selected ASS tracks and font attachments.
- libass renders the complete subtitle composition in memory.
- `mkvmerge` copies the original Matroska streams and appends generated PGS.

Video and audio are never decoded or transcoded.

## Conversion flow

```text
Matroska probe
  -> selected ASS tracks
  -> ASS events and temporal classification
  -> timeline of active-event sets
  -> render plan
  -> direct libass RGBA composition
  -> visible-alpha crop
  -> PGS palette quantization
  -> PGS RLE and SUP segments
  -> atomic Matroska mux
```

Events containing temporal ASS tags such as `\move`, `\t`, `\fad`, `\fade`,
karaoke tags, Banner, or Scroll effects are dynamic. A static timeline state is
rendered once. Contiguous dynamic states are sampled at the configured rate and
emit a new PGS object only when libass reports or produces a bitmap change.

The renderer always gives libass the complete ASS track at the requested
timestamp. Events are never rendered independently, preserving layer order,
overlap, clipping, blur, transforms, and alpha composition.

## PGS encoding

`pgs.py` converts a cropped RGBA bitmap into a palette and indexed image.
`sup.py` writes presentation, window, palette, object, and end segments. PGS
object data uses the format's run-length encoding and is segmented when needed.

PGS is palette-based, so conversion quantizes the rendered RGBA colors. Cropping
happens first to constrain both quantization and RLE work to visible pixels.

## Incremental identity

A generated track title is the ASS title plus ` (PGS)`. A PGS corresponds to an
ASS source when title, language, default flag, and forced flag all match. This
avoids treating an unrelated PGS in the release as sufficient merely because
one exists.

Library resume state also records the selected-track filter and fingerprints of
both source and destination. Changing the source, destination, or selector
causes the file to be evaluated again.

## Failure safety

SUP data is written as `.partial` and promoted only after a successful track
conversion. Failed group output is marked `.failed` when retained for
diagnostics. Matroska muxing uses a unique sibling temporary file and
`os.replace`, so an existing destination is not removed before a complete new
file is available.

Library workers isolate exceptions per MKV. Ctrl+C sets a cooperative
cancellation signal, cancels queued futures, and waits for active files to stop
at a conversion boundary.
