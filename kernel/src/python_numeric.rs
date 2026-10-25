//! Decimal wire compatibility for former Python configuration and HTTP inputs.
//! This is lexical normalization only; each caller retains its existing bounds.

/// Normalize Python decimal int(str) syntax, including Unicode 16 Nd blocks,
/// sign, surrounding whitespace, single underscores and the default 4300-digit
/// conversion limit. Preserve magnitude as text rather than rounding via f64.
#[allow(clippy::result_unit_err)]
pub fn integer_text(value: &str) -> Result<String, ()> {
    const ZEROS: &[u32] = &[
        48, 1632, 1776, 1984, 2406, 2534, 2662, 2790, 2918, 3046, 3174, 3302, 3430, 3558, 3664,
        3792, 3872, 4160, 4240, 6112, 6160, 6470, 6608, 6784, 6800, 6992, 7088, 7232, 7248, 42528,
        43216, 43264, 43472, 43504, 43600, 44016, 65296, 66720, 68912, 68928, 69734, 69872, 69942,
        70096, 70384, 70736, 70864, 71248, 71360, 71376, 71386, 71472, 71904, 72016, 72688, 72784,
        73040, 73120, 73552, 90416, 92768, 92864, 93008, 93552, 118000, 120782, 120792, 120802,
        120812, 120822, 123200, 123632, 124144, 124401, 125264, 130032,
    ];
    let value = value.trim();
    let (sign, digits) = if let Some(value) = value.strip_prefix('-') {
        ("-", value)
    } else {
        ("", value.strip_prefix('+').unwrap_or(value))
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return Err(());
    }
    let mut normalized = sign.to_owned();
    for c in digits.chars().filter(|c| *c != '_') {
        let code = c as u32;
        let digit = ZEROS
            .iter()
            .find_map(|zero| (code >= *zero && code < *zero + 10).then(|| code - zero))
            .ok_or(())?;
        normalized.push(char::from(b'0' + digit as u8));
    }
    if normalized.len() - sign.len() > 4300 {
        return Err(());
    }
    Ok(normalized)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decimal_grammar_retains_wide_values_and_unicode() {
        for (input, expected) in [
            (" +١_２ ", "12"),
            ("\u{85}-００1", "-001"),
            ("\u{1e5f2}", "1"),
        ] {
            assert_eq!(integer_text(input), Ok(expected.into()));
        }
        assert_eq!(integer_text(&"9".repeat(4300)), Ok("9".repeat(4300)));
        for input in [
            "",
            "+",
            "_1",
            "1_",
            "1__2",
            "²",
            "1.0",
            "\u{1c}1",
            "1 2",
            &"9".repeat(4301),
        ] {
            assert!(integer_text(input).is_err(), "{input:?}");
        }
    }
}
