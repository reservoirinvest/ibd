# Programs for optimized sowing, covering, protecting, reaping and de-orphaning SNPs

The programs are structured along the following lines:

## build.py

- builds underlyings and option chains

## classify.py

* classifes portfolio, underlyings and open orders to states, as per state logic given below

## derive.py

* gets the best set of options orders for covering, sowing, protecting, reaping and de-orphaning.

## execute.py

* executes the order pickles generated - like df_nkd, df_protect, df_reap.

## guage.py

* it reports the health of the portfolio and the logic data structures.

# State logic is as follows

## Identifying states

There are three dataframes viz: pf, df_openords and df_unds.
Each of them have the following fields:

- symbol: for name of the symbol
- secType: with STK for stock, OPT for option
- right: with P for put and C for call. Only secType == 'OPT' will have right.
- action: with SELL or BUY
- position: an integer that can be positive or negative

### Portfolio state

Portfolio states are derived from dataframe 'pf'

**Note**: Portfolio has 'state' field.

    - 'zen': Perfect. Stock with both covering and protecting option positions

    - 'exposed': Stock positions without any covering or protecting options
    - 'unprotected': Stock with only covering option position
    - 'uncovered': Stock with only protecting options position

    - 'straddled': Matching call and put options with no underlying stock
      - ... straddles are for stocks having earnings declaration within naked time horizon

    - 'covering': Short calls or puts with underlying stock
    - 'protecting': Long calls or puts with underlying stock
    - 'sowed': Short options without matching stock positions
    - 'orphaned': Long options without matching stock position

### Order state

Order states are derived from df_openords

    - 'covering' - an option order symbol with action: SELL, having an underlying stock position derived from pf dataframe
    - 'protecting' - an option order symbol with action: BUY, having an underlying stock position derived from pf dataframe
    - 'sowing' - an option order with action: SELL, having no underlying stock position
    - 'reaping' - an option order with action: BUY, having an underlying option position for the same right and strike
    - 'straddling' - two option orders with action: BUY for the same symbol, not in portfolio position
    - 'de-orphaning' - an option order with action: SELL having no underlying stock or any option position

### Symbol States

* Symbol state are derived from portfolio state and order state. They are reflected in df_unds.

  - 'zen': symbol
    - has a stock both covering and protecting portfolio positions or orders
    - has 'straddled' portfolio state
    - has a short 'sowing' order
    - is in 'unprotected' portfolio state with a 'protecting' order
    - is in 'uncovered' portfolio state with a 'covering' order
    - has long option 'orphaned' position with an open 'de-orphaning' order
    - has short option 'sowed' position with a on open 'reaping' order
  - 'unreaped': Symbol has a short option position with no open 'reaping' order
  - 'exposed': Symbol has a stock, but has not covering or protecting order or option position
  - 'uncovered': Symbol has a stock that is protected, but not covered
  - 'unprotected': Symbol has a stock that is covered, but not protected
  - 'virgin': Symbol is not sowed and ready for naked orders
  - 'orphaned': Symbol has a put or call buy position, but without any underlying
  - 'unknown': Anything that is not in any one of the above states. (Should not be there!!
