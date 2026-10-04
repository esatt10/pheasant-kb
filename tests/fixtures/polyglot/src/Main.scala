package acme

import acme.store.Store
import scala.collection.mutable.{Map, Set}

trait Runner { def run(): Unit }

object Main extends Runner {
  def run(): Unit = Store.open("x")
}
