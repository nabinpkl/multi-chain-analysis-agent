const DEFAULT_API_URL = "http://localhost:8002";

export function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

export interface RawEdge {
  signature: string;
  block_time: number;
  from: string;
  to: string;
  /**
   * SOL volume only. Always 0 for SPL transfers (`mint` present);
   * the wire format omits the field for native SOL so it arrives as 0.
   */
  volume_sol: number;
  /**
   * SPL/Token-2022 mint pubkey if this edge represents a token
   * transfer. Absent for native SOL. Treated as opaque.
   */
  mint?: string;
  /**
   * `"mint"` for token issuance edges (`from` is the mint pubkey,
   * tokens flowed to `to`), `"burn"` for destruction edges (`to` is
   * the mint pubkey, tokens flowed from `from` and were destroyed).
   * Absent for regular transfers.
   */
  kind?: "mint" | "burn";
}
