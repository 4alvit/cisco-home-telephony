# Lake dawn wallpaper

An original landscape generated with the built-in OpenAI image-generation tool
on October 17, 2026. No Cisco artwork or firmware is included.

- `lake-dawn-source.png`: original generated artwork.
- `cisco-7945g/home.png`: 320 × 212, RGB PNG, 8 bits per channel.
- `cisco-7945g/home-thumb.png`: 80 × 53 thumbnail, same encoding.
- `cisco-7945g/List.xml`: the single-image Cisco background catalogue.

![Phone-size preview](cisco-7945g/home.png)

See [installation and second-line instructions](../../docs/second-line-and-wallpaper.md).
The files are served at `Desktops/320x212x16/`. The `x16` describes the phone's
display; it does not mean 16-bit-per-channel PNG encoding. Selection requires
Settings → User Preferences → Background Images → Select → Save on the phone.

## Reproduce device encoding

The source was generated, then ImageMagick performed only the device-size crop,
resampling and RGB encoding:

```bash
magick assets/wallpapers/lake-dawn-source.png -resize '320x212^' \
  -gravity center -extent 320x212 -alpha off -depth 8 \
  -define png:exclude-chunk=all PNG24:assets/wallpapers/cisco-7945g/home.png
magick assets/wallpapers/cisco-7945g/home.png -resize 80x53 \
  -alpha off -depth 8 -define png:exclude-chunk=all \
  PNG24:assets/wallpapers/cisco-7945g/home-thumb.png
```

## Generation prompt

> Use case: stylized-concept.
> Asset type: an original wallpaper for the small color screen of a Cisco 7945G telephone in a home, not a mockup of a telephone.
> Primary request: a beautiful, welcoming, gently luminous illustrated landscape that makes an old home telephone feel personal and delightful.
> Composition: landscape aspect ratio approximately 320:212 (about 3:2). A calm dawn over softly layered hills and a quiet lake; elegant broad forms, a simple focal point, subtle atmosphere, exceptionally clean silhouette shapes. Keep the entire image visually calm and legible when reduced to only 320 by 212 pixels, with smooth uncluttered areas that will remain usable behind small telephone interface labels. Low visual noise; no tiny details.
> Style: polished original digital illustration, tasteful and warm, softly dimensional but not photorealistic. A complete edge-to-edge scene without any device frame, border or montage.
> Constraints: no text, no letters, no logos, no watermark, no people, no user-interface elements. Opaque background.
