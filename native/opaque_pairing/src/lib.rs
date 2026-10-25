//! Narrow in-process OPAQUE adapter. No key derivation, curve arithmetic,
//! confirmation MAC, secret serialization, filesystem or network implementation.
//! Registration is entirely local to the node displaying an ephemeral PIN.
//! Only RFC 9807 KE1/KE2/KE3 leave this adapter.
use opaque_ke::{
    CipherSuite, ClientLogin, ClientLoginFinishParameters, ClientRegistration,
    ClientRegistrationFinishParameters, CredentialFinalization, CredentialRequest,
    CredentialResponse, Identifiers, ServerLogin, ServerLoginParameters, ServerRegistration,
    ServerSetup,
    argon2::{Algorithm, Argon2, Params, Version},
    rand::rngs::OsRng,
};
use zeroize::{Zeroize, Zeroizing};

pub const BACKEND_ID: &str = "OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1";
struct Suite;
impl CipherSuite for Suite {
    type OprfCs = opaque_ke::Ristretto255;
    type KeyExchange = opaque_ke::TripleDh<opaque_ke::Ristretto255, sha2::Sha512>;
    type Ksf = Argon2<'static>;
}

type PairingResult<T> = std::result::Result<T, &'static str>;
fn failure<E>(_: E) -> &'static str {
    "PAIRING_FAILED"
}
fn ksf() -> Argon2<'static> {
    // Fixed data-independent addressing; no password-dependent Argon2d accesses.
    Argon2::new(
        Algorithm::Argon2i,
        Version::V0x13,
        Params::new(65536, 3, 1, None).expect("fixed Argon2i parameters"),
    )
}
fn pin(value: &[u8]) -> PairingResult<Zeroizing<Vec<u8>>> {
    if value.len() != 4 || !value.iter().all(u8::is_ascii_digit) {
        return Err(failure(()));
    }
    Ok(Zeroizing::new(value.to_vec()))
}
fn bound(context: &[u8], client: &[u8], server: &[u8]) -> PairingResult<()> {
    if context.is_empty()
        || context.len() > 4096
        || client.is_empty()
        || server.is_empty()
        || client.len() > 2048
        || server.len() > 2048
        || client == server
    {
        return Err(failure(()));
    }
    Ok(())
}

pub struct Client {
    state: Option<ClientLogin<Suite>>,
    code: Zeroizing<Vec<u8>>,
    context: Vec<u8>,
    client: Vec<u8>,
    server: Vec<u8>,
    message: Option<Vec<u8>>,
}
impl Client {
    pub fn new(
        code: &[u8],
        context: Vec<u8>,
        client: Vec<u8>,
        server: Vec<u8>,
    ) -> PairingResult<Self> {
        bound(&context, &client, &server)?;
        let code = pin(code)?;
        let start = ClientLogin::<Suite>::start(&mut OsRng, &code).map_err(failure)?;
        Ok(Self {
            state: Some(start.state),
            code,
            context,
            client,
            server,
            message: Some(start.message.serialize().to_vec()),
        })
    }
    pub fn start_message(&mut self) -> PairingResult<Vec<u8>> {
        self.message.take().ok_or_else(|| failure(()))
    }
    pub fn finish_message(&mut self, inbound: &[u8]) -> PairingResult<Vec<u8>> {
        let state = self.state.take().ok_or_else(|| failure(()))?;
        // Consume on every finish attempt, including malformed messages.
        let code = std::mem::replace(&mut self.code, Zeroizing::new(Vec::new()));
        let response = CredentialResponse::<Suite>::deserialize(inbound).map_err(failure)?;
        let hardener = ksf();
        let params = ClientLoginFinishParameters::new(
            Some(&self.context),
            Identifiers {
                client: Some(&self.client),
                server: Some(&self.server),
            },
            Some(&hardener),
        );
        let mut result = state
            .finish(&mut OsRng, &code, response, params)
            .map_err(failure)?;
        let message = result.message.serialize().to_vec();
        result.session_key.zeroize();
        result.export_key.zeroize();
        Ok(message)
    }
}

pub struct Server {
    state: Option<ServerLogin<Suite>>,
    context: Vec<u8>,
    client: Vec<u8>,
    server: Vec<u8>,
    message: Option<Vec<u8>>,
}
impl Server {
    pub fn new(
        code: &[u8],
        context: Vec<u8>,
        client: Vec<u8>,
        server: Vec<u8>,
        inbound: &[u8],
    ) -> PairingResult<Self> {
        bound(&context, &client, &server)?;
        let code = pin(code)?;
        let request = CredentialRequest::<Suite>::deserialize(inbound).map_err(failure)?;
        let mut rng = OsRng;
        let setup = ServerSetup::<Suite>::new(&mut rng);
        // Standard registration, in memory at the PIN-owning node. The remote
        // peer cannot submit a registration record or choose a server secret.
        let registration = ClientRegistration::<Suite>::start(&mut rng, &code).map_err(failure)?;
        let response = ServerRegistration::<Suite>::start(&setup, registration.message, &context)
            .map_err(failure)?;
        let mut uploaded = registration
            .state
            .finish(
                &mut rng,
                &code,
                response.message,
                ClientRegistrationFinishParameters::new(
                    Identifiers {
                        client: Some(&client),
                        server: Some(&server),
                    },
                    Some(&ksf()),
                ),
            )
            .map_err(failure)?;
        uploaded.export_key.zeroize();
        let record = ServerRegistration::finish(uploaded.message);
        let login = ServerLogin::<Suite>::start(
            &mut rng,
            &setup,
            Some(record),
            request,
            &context,
            ServerLoginParameters {
                context: Some(&context),
                identifiers: Identifiers {
                    client: Some(&client),
                    server: Some(&server),
                },
            },
        )
        .map_err(failure)?;
        Ok(Self {
            state: Some(login.state),
            context,
            client,
            server,
            message: Some(login.message.serialize().to_vec()),
        })
    }
    pub fn start_message(&mut self) -> PairingResult<Vec<u8>> {
        self.message.take().ok_or_else(|| failure(()))
    }
    pub fn finish(&mut self, inbound: &[u8]) -> PairingResult<()> {
        let state = self.state.take().ok_or_else(|| failure(()))?;
        let finalization =
            CredentialFinalization::<Suite>::deserialize(inbound).map_err(failure)?;
        let mut result = state
            .finish(
                finalization,
                ServerLoginParameters {
                    context: Some(&self.context),
                    identifiers: Identifiers {
                        client: Some(&self.client),
                        server: Some(&self.server),
                    },
                },
            )
            .map_err(failure)?;
        result.session_key.zeroize();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rand::Rng;
    fn code() -> Vec<u8> {
        format!("{:04}", OsRng.gen_range(0..10000)).into_bytes()
    }
    #[test]
    fn mutual_confirmation_and_single_use() {
        let code = code();
        let mut client =
            Client::new(&code, b"session".to_vec(), b"A".to_vec(), b"B".to_vec()).unwrap();
        let ke1 = client.start_message().unwrap();
        let mut server = Server::new(
            &code,
            b"session".to_vec(),
            b"A".to_vec(),
            b"B".to_vec(),
            &ke1,
        )
        .unwrap();
        let ke2 = server.start_message().unwrap();
        let ke3 = client.finish_message(&ke2).unwrap();
        server.finish(&ke3).unwrap();
        assert!(client.code.is_empty());
        assert!(client.finish_message(&ke2).is_err());
        assert!(server.finish(&ke3).is_err());
    }
    #[test]
    fn context_mismatch_fails_and_consumes_client_secret() {
        let code = code();
        let mut client = Client::new(&code, b"one".to_vec(), b"A".to_vec(), b"B".to_vec()).unwrap();
        let ke1 = client.start_message().unwrap();
        let mut server =
            Server::new(&code, b"two".to_vec(), b"A".to_vec(), b"B".to_vec(), &ke1).unwrap();
        let ke2 = server.start_message().unwrap();
        assert!(client.finish_message(&ke2).is_err());
        assert!(client.code.is_empty());
    }
    #[test]
    fn malformed_finish_consumes_state_and_code() {
        let code = code();
        let mut client = Client::new(&code, b"one".to_vec(), b"A".to_vec(), b"B".to_vec()).unwrap();
        assert!(client.finish_message(b"malformed").is_err());
        assert!(client.state.is_none() && client.code.is_empty());
    }
}
