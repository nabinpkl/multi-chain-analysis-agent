use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Duration;

use governor::clock::DefaultClock;
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter};
use reqwest::Client;
use serde_json::{Value, json};

use super::error::RpcError;
use super::types::{Block, JsonRpcResponse};

type Limiter = RateLimiter<NotKeyed, InMemoryState, DefaultClock>;

#[derive(Clone)]
pub struct RpcClient {
    http: Client,
    url: String,
    limiter: Arc<Limiter>,
}

impl RpcClient {
    pub fn new(url: String, min_interval: Duration) -> Self {
        let interval = if min_interval.is_zero() {
            Duration::from_millis(1)
        } else {
            min_interval
        };
        let quota = Quota::with_period(interval)
            .expect("min_interval must be > 0")
            .allow_burst(NonZeroU32::new(1).unwrap());
        let limiter = Arc::new(RateLimiter::direct(quota));
        Self {
            http: Client::new(),
            url,
            limiter,
        }
    }

    async fn call<T: serde::de::DeserializeOwned>(
        &self,
        method: &str,
        params: Value,
    ) -> Result<T, RpcError> {
        self.limiter.until_ready().await;

        let body = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        });

        let resp = self
            .http
            .post(&self.url)
            .json(&body)
            .send()
            .await
            .map_err(|e| RpcError::Transient(e.without_url().to_string()))?;

        let status = resp.status();
        if status.as_u16() == 429 {
            return Err(RpcError::RateLimited);
        }
        if status.is_server_error() {
            return Err(RpcError::Transient(format!("http {}", status)));
        }
        if !status.is_success() {
            return Err(RpcError::Fatal(format!("http {}", status)));
        }

        let parsed: JsonRpcResponse<T> = resp
            .json()
            .await
            .map_err(|e| RpcError::Fatal(format!("decode: {}", e.without_url())))?;

        if let Some(err) = parsed.error {
            return Err(map_jsonrpc_error(err.code, err.message));
        }

        parsed
            .result
            .ok_or_else(|| RpcError::Fatal("missing result".into()))
    }

    pub async fn get_slot(&self) -> Result<u64, RpcError> {
        self.call("getSlot", json!([{ "commitment": "confirmed" }])).await
    }

    pub async fn get_block(&self, slot: u64) -> Result<Block, RpcError> {
        self.call(
            "getBlock",
            json!([
                slot,
                {
                    "encoding": "jsonParsed",
                    "transactionDetails": "full",
                    "rewards": false,
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0
                }
            ]),
        )
        .await
    }
}

fn map_jsonrpc_error(code: i64, message: String) -> RpcError {
    match code {
        -32007 => RpcError::SkippedSlot,
        -32004 | -32014 => RpcError::NotYetAvailable,
        -32005 | -32016 => RpcError::Transient(message),
        _ => RpcError::Fatal(format!("code {}: {}", code, message)),
    }
}
