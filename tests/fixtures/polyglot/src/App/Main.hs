module App.Main where

import qualified Data.Map as Map
import App.Store (load)

-- import Ghost.Module

data Item = Item { itemId :: Int }

render :: Item -> String
render item = show (itemId item)

main :: IO ()
main = load >>= print
