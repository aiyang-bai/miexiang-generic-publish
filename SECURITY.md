# Security

Do not attach credentials, real profiles, unpublished artifacts or private machine paths to public issues. If the repository has GitHub private vulnerability reporting enabled after publication, use its private report channel. Otherwise a private reporting channel is not yet established; do not publish exploit details containing secrets. There is no promised response SLA.

The initial candidate is the only maintained development line. No signed release or supported production version is claimed. Byte verification establishes equality to an expected hash; it does not establish artifact safety, authenticity of an untrusted hash, signing, or repository access control. Use least-privilege authorized repositories, OS credential stores and protected data directories. Separate data roots/machines do not share local version locks. Never delete uncertain retained publication locks or retry uncertain side effects blindly.
