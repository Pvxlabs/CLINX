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
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};
use zeroize::{Zeroize, Zeroizing};

const BACKEND_ID: &str = "OPAQUE-3DH-RISTRETTO255-SHA512-ARGON2I-v1";
struct Suite;
impl CipherSuite for Suite {
    type OprfCs = opaque_ke::Ristretto255;
    type KeyExchange = opaque_ke::TripleDh<opaque_ke::Ristretto255, sha2::Sha512>;
    type Ksf = Argon2<'static>;
}

fn failure<E>(_: E) -> PyErr {
    PyValueError::new_err("PAIRING_FAILED")
}
fn ksf() -> Argon2<'static> {
    // Fixed data-independent addressing; no password-dependent Argon2d accesses.
    Argon2::new(
        Algorithm::Argon2i,
        Version::V0x13,
        Params::new(65536, 3, 1, None).expect("fixed Argon2i parameters"),
    )
}
fn pin(value: &[u8]) -> PyResult<Zeroizing<Vec<u8>>> {
    if value.len() != 4 || !value.iter().all(u8::is_ascii_digit) {
        return Err(failure(()));
    }
    Ok(Zeroizing::new(value.to_vec()))
}
fn bound(context: &[u8], client: &[u8], server: &[u8]) -> PyResult<()> {
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

#[pyclass]
struct Client {
    state: Option<ClientLogin<Suite>>,
    code: Zeroizing<Vec<u8>>,
    context: Vec<u8>,
    client: Vec<u8>,
    server: Vec<u8>,
    message: Option<Vec<u8>>,
}
#[pymethods]
impl Client {
    #[new]
    fn new(code: &[u8], context: Vec<u8>, client: Vec<u8>, server: Vec<u8>) -> PyResult<Self> {
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
    fn start<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let message = self.message.take().ok_or_else(|| failure(()))?;
        Ok(PyBytes::new(py, &message))
    }
    fn finish<'py>(&mut self, py: Python<'py>, inbound: &[u8]) -> PyResult<Bound<'py, PyBytes>> {
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
        let message = PyBytes::new(py, &result.message.serialize());
        result.session_key.zeroize();
        result.export_key.zeroize();
        Ok(message)
    }
}

#[pyclass]
struct Server {
    state: Option<ServerLogin<Suite>>,
    context: Vec<u8>,
    client: Vec<u8>,
    server: Vec<u8>,
    message: Option<Vec<u8>>,
}
#[pymethods]
impl Server {
    #[new]
    fn new(
        code: &[u8],
        context: Vec<u8>,
        client: Vec<u8>,
        server: Vec<u8>,
        inbound: &[u8],
    ) -> PyResult<Self> {
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
    fn start<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let message = self.message.take().ok_or_else(|| failure(()))?;
        Ok(PyBytes::new(py, &message))
    }
    fn finish(&mut self, inbound: &[u8]) -> PyResult<()> {
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

#[pymodule]
fn _clinx_opaque(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("BACKEND_ID", BACKEND_ID)?;
    m.add("OPAQUE_KE_VERSION", "4.0.1")?;
    m.add_class::<Client>()?;
    m.add_class::<Server>()?;
    Ok(())
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
