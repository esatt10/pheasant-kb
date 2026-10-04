mod parser;
pub mod model;
use crate::model::item::Item;
use std::collections::HashMap;
extern crate serde;

/// fn ghost() {}
pub const MAX_ITEMS: usize = 10;

pub struct Engine {
    items: HashMap<String, Item>,
}

pub trait Runner {
    fn run(&self);
}

impl Engine {
    pub fn load(&mut self) -> usize {
        let parsed = parser::parse("fn fake()");
        self.items.len() + helper(parsed)
    }
}

fn helper(value: usize) -> usize {
    value
}
