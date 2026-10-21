# Independent accounts, telemetry and acceptance
Status: resolved

Implement native API boundary, durable telemetry and subscription fetch metadata; CLI/API visibility; batch replacement. Test isolated normal traffic and account separation. Deploy only Ali ingress/control components, retire old credentials, verify two new subscriptions and return desktop URL. Record checks and limitations. Fault/reboot tests deferred.

## Result

Implemented, deployed and validated. See `docs/validation/device-usage-2026-10-21/README.md`. Old token-only revocation was insufficient; subsequently revoked shared account and verified actual authentication failure. Two separate new subscriptions issued; usage collector active. Physical client imports pending user, fault/reboot tests deferred.
