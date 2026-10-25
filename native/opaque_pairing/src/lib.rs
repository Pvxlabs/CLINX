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
#[cfg(feature = "python")]
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};
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
#[cfg(feature = "python")]
fn py_error(code: &'static str) -> PyErr {
    PyValueError::new_err(code)
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

#[cfg_attr(feature = "python", pyclass)]
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

#[cfg_attr(feature = "python", pyclass)]
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

#[cfg(feature = "python")]
#[pymethods]
impl Client {
    #[new]
    fn py_new(code: &[u8], context: Vec<u8>, client: Vec<u8>, server: Vec<u8>) -> PyResult<Self> {
        Self::new(code, context, client, server).map_err(py_error)
    }
    fn start<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        self.start_message()
            .map(|v| PyBytes::new(py, &v))
            .map_err(py_error)
    }
    fn finish<'py>(&mut self, py: Python<'py>, inbound: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
        self.finish_message(inbound)
            .map(|v| PyBytes::new(py, &v))
            .map_err(py_error)
    }
}
#[cfg(feature = "python")]
#[pymethods]
impl Server {
    #[new]
    fn py_new(
        code: &[u8],
        context: Vec<u8>,
        client: Vec<u8>,
        server: Vec<u8>,
        inbound: &[u8],
    ) -> PyResult<Self> {
        Self::new(code, context, client, server, inbound).map_err(py_error)
    }
    fn start<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        self.start_message()
            .map(|v| PyBytes::new(py, &v))
            .map_err(py_error)
    }
    #[pyo3(name = "finish")]
    fn py_finish(&mut self, inbound: &[u8]) -> PyResult<()> {
        self.finish(inbound).map_err(py_error)
    }
}
#[cfg(feature = "python")]
#[pymodule]
fn _clinx_opaque(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("BACKEND_ID", BACKEND_ID)?;
    m.add("OPAQUE_KE_VERSION", "4.0.1")?;
    m.add_class::<Client>()?;
    m.add_class::<Server>()?;
    Ok(())
}

#[cfg(all(test, feature = "python"))]
mod tests {
    use super::*;
    use rand::Rng;

    fn code() -> Vec<u8> {
        format!("{:04}", OsRng.gen_range(0..10000)).into_bytes()
    }

    #[test]
    fn mutual_confirmation_and_single_use() {
        Python::initialize();
        Python::attach(|py| {
            let code = code();
            let mut client =
                Client::new(&code, b"session".to_vec(), b"A".to_vec(), b"B".to_vec()).unwrap();
            let ke1 = client.start(py).unwrap();
            let mut server = Server::new(
                &code,
                b"session".to_vec(),
                b"A".to_vec(),
                b"B".to_vec(),
                ke1.as_bytes(),
            )
            .unwrap();
            let ke2 = server.start(py).unwrap();
            let ke3 = client.finish(py, ke2.as_bytes()).unwrap();
            server.finish(ke3.as_bytes()).unwrap();
            assert!(client.code.is_empty());
            assert!(client.finish(py, ke2.as_bytes()).is_err());
            assert!(server.finish(ke3.as_bytes()).is_err());
        });
    }

    #[test]
    fn context_mismatch_fails_and_consumes_client_secret() {
        Python::initialize();
        Python::attach(|py| {
            let code = code();
            let mut client =
                Client::new(&code, b"one".to_vec(), b"A".to_vec(), b"B".to_vec()).unwrap();
            let ke1 = client.start(py).unwrap();
            let mut server = Server::new(
                &code,
                b"two".to_vec(),
                b"A".to_vec(),
                b"B".to_vec(),
                ke1.as_bytes(),
            )
            .unwrap();
            let ke2 = server.start(py).unwrap();
            assert!(client.finish(py, ke2.as_bytes()).is_err());
            assert!(client.code.is_empty());
        });
    }

    #[test]
    fn malformed_finish_consumes_state_and_code() {
        Python::initialize();
        Python::attach(|py| {
            let code = code();
            let mut client =
                Client::new(&code, b"one".to_vec(), b"A".to_vec(), b"B".to_vec()).unwrap();
            assert!(client.finish(py, b"malformed").is_err());
            assert!(client.state.is_none() && client.code.is_empty());
        });
    }
}
