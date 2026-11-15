/** Local product artwork. No network lookup or runtime icon service.
 *
 * Modified for mattermost-ai-agent-bus: the third-party brand artwork of the
 * Stageshow kit (kit/brands/*.svg) is left out, since this site shows no
 * other product's mark. The registry is empty; the types stay so BrandMark
 * still compiles. */
export type BrandAsset = {
  name: string
  category: 'Payments' | 'Banks' | 'Apps' | 'Crypto'
  light: string
  dark: string
  tileArtwork?: string
  tileBackground?: string
  proportion: number
  scale: number
}

export const brandMarks: Record<string, BrandAsset> = {}

export type BrandName = string
